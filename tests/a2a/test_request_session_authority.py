"""Real AU session minting and A2A dispatch with scripted credential verification.

No live issuer, TLS, production EG durability, or signed approval is qualified.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from arbora.a2a import mcp as a2a_mcp
from arbora.a2a.application import AmbientA2AAuthenticator
from arbora.a2a.service import A2AService
from arbora.api.contracts.session import current_session, use_session
from arbora.api.registry import Surface
from arbora.core.settings import runtime_config as config
from arbora.mcp_server import runtime as mcp_runtime
from arbora.security import request_identity
from arbora.security.actor_context import (
    ActorContext,
    ActorType,
    current_actor,
    use_actor,
)
from tests.a2a._support import authenticated_operation_app
from tests.a2a.test_application import Authority, Router
from tests.a2a.test_mcp import _Mcp
from tests.a2a.test_operation_bridge import (
    SessionRuntime,
    assert_no_effect,
    post,
    session,
)
from tests.api.test_invoke_contract import operation, setup_services

pytestmark = pytest.mark.spec("GRAPHOS-A2A-R006.2.1.1")


def fixture(monkeypatch: pytest.MonkeyPatch, *, failure: str = "") -> Any:
    # Existing configuration only; AU's actual public session minter executes.
    monkeypatch.setattr(config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(config, "kg_policy_version", "policy:fixture")
    verified: list[Any] = []
    requests: list[Any] = []

    async def credential_owner(credential: str) -> ActorContext:
        if failure == "verifier-http500":
            raise HTTPException(status_code=500, detail="fixture verifier unavailable")
        if failure == "verifier-config":
            raise RuntimeError("fixture verifier configuration unavailable")
        if (
            credential != "fixture-verified" or failure == "credential"
        ):  # sanitizer:ignore - synthetic ASGI credential
            raise HTTPException(status_code=401)
        await asyncio.sleep(0)
        if failure == "header-mutation":
            requests[-1]["headers"] = [
                (b"authorization", b"Bearer substituted")
            ]  # sanitizer:ignore - synthetic ASGI credential
        actor = ActorContext(
            actor_id="person:one",
            actor_type=ActorType.AUTOMATED_SERVICE
            if failure == "automated-service"
            else ActorType.HUMAN,
            tenant_id="tenant:one",
            authenticated=True,
            roles=("kg:read",) if failure == "scope" else ("kg:write",),
            credential_expires_at=1
            if failure == "expired"
            else int(time.time()) + 3600,
        )
        verified.append(actor)
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    services, _runtime, audit, journal, _leases = setup_services(
        operation(scopes=frozenset({"kg:write"}), surfaces=frozenset({Surface.A2A}))
    )
    runtime = SessionRuntime()
    runtime.change_caller = failure == "mismatched"
    services = replace(services, runtime=runtime)
    app = authenticated_operation_app(services)

    async def capture(scope: Any, receive: Any, send: Any) -> None:
        requests.append(scope)
        # Arbitrary request state is deliberately insufficient credential evidence.
        scope.setdefault("state", {})["user_claims"] = {
            "auth_type": "jwt",
            "sub": "person:forged",
            "tenant_id": "tenant:one",
            "scope": "kg:write",
            "exp": int(time.time()) + 3600,
        }
        await app(scope, receive, send)

    return capture, runtime, audit, journal, requests, verified


def test_public_au_minter_reaches_pipeline_and_rechecks_every_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, runtime, audit, journal, _requests, verified = fixture(monkeypatch)
    response = post(app)
    assert response.status_code == 200
    assert response.json()["result"]["result"] == {"value": 7}
    assert len(verified) == len(runtime.calls) == 1
    bound = runtime.callers[0].session
    assert bound.actor is verified[0]
    assert bound.scopes == frozenset({"kg:read", "kg:write"})
    assert all(caller.session is bound for caller in runtime.callers)
    assert audit.events[0]["surface"] == Surface.A2A.value
    assert len(journal.completions) == 1
    first_caller_count = len(runtime.callers)
    assert post(app).status_code == 200
    assert len(verified) == 2 and len(runtime.calls) == 1
    assert runtime.callers[first_caller_count].session is not bound
    assert runtime.callers[first_caller_count].session.actor is verified[1]


@pytest.mark.parametrize(
    "failure",
    [
        "credential",
        "scope",
        "expired",
        "mismatched",
        "header-mutation",
        "automated-service",
    ],
)
def test_denied_or_changed_credential_has_zero_effects(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    app, runtime, audit, journal, _requests, _verified = fixture(
        monkeypatch, failure=failure
    )
    response = post(app)
    assert response.status_code in {401, 403}
    assert_no_effect(runtime, audit, journal)


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [
            ("Authorization", "Bearer unknown")
        ],  # sanitizer:ignore - synthetic ASGI credential
        [("Authorization", "Basic fixture-verified")],
        [
            ("Authorization", "Bearer fixture-verified extra")
        ],  # sanitizer:ignore - synthetic ASGI credential
        [
            (
                "Authorization",
                "Bearer fixture-verified",
            ),  # sanitizer:ignore - synthetic ASGI credential
            (
                "Authorization",
                "Bearer fixture-verified",
            ),  # sanitizer:ignore - synthetic ASGI credential
        ],
    ],
)
def test_ambient_session_and_forged_state_never_replace_exact_bearer(
    monkeypatch: pytest.MonkeyPatch,
    headers: Any,
) -> None:
    app, runtime, audit, journal, _requests, _verified = fixture(monkeypatch)

    async def execute() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            return await client.post(
                "/a2a",
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": "foreign-request",
                    "method": "arbora.op/invoke",
                    "params": {
                        "op": "example.write",
                        "params": {},
                        "idempotencyKey": "foreign",
                    },
                },
            )

    with use_session(session(frozenset({"kg:write"}))):
        response = asyncio.run(execute())
    assert response.status_code == 401
    assert_no_effect(runtime, audit, journal)


def test_previous_verified_request_cannot_authorize_a_new_bad_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, runtime, audit, journal, _requests, verified = fixture(monkeypatch)
    assert post(app).status_code == 200
    calls, events, rows = len(runtime.calls), len(audit.events), dict(journal.rows)
    with use_session(runtime.callers[0].session):
        response = post(
            app,
            token="borrowed-authority",  # sanitizer:ignore - synthetic ASGI credential
        )
    assert response.status_code == 401
    assert len(verified) == 1
    assert len(runtime.calls) == calls and len(audit.events) == events
    assert journal.rows == rows


def test_verified_session_does_not_enable_unsigned_plan_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, runtime, audit, journal, _requests, _verified = fixture(monkeypatch)
    response = post(
        app,
        params={
            "op": "example.write",
            "params": {},
            "planRef": "plan:one",
            "idempotencyKey": "effect:one",
        },
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == -32011
    assert_no_effect(runtime, audit, journal)


def registered_unary(monkeypatch: pytest.MonkeyPatch, failure: str = "") -> Any:
    _app, _runtime, _audit, _journal, _requests, verified = fixture(
        monkeypatch,
        failure=failure,
    )
    observed = []

    class RecordingAuthority(Authority):
        async def get(self, task_id: str) -> Any:
            observed.append((current_session(), current_actor()))
            if failure == "authority":
                raise ValueError("fixture authority refusal")
            return await super().get(task_id)

        async def cancel(self, task_id: str) -> Any:
            observed.append((current_session(), current_actor()))
            return await super().cancel(task_id)

    authority = RecordingAuthority()
    monkeypatch.setattr(
        a2a_mcp, "_service", lambda: A2AService(authority=authority, router=Router())
    )
    monkeypatch.setattr(mcp_runtime, "REGISTERED_TOOLS", {})
    registrar = _Mcp()
    a2a_mcp.register_a2a_tools(registrar)
    app = FastAPI()
    for (path, methods), handler in registrar.routes.items():
        app.add_api_route(path, handler, methods=list(methods))
    return app, authority, observed, verified


def unary_request(app: Any, method: str, params: Any, *, missing: bool = False) -> Any:
    ambient = session(frozenset({"kg:write"}))

    async def execute() -> httpx.Response:
        with use_session(ambient), use_actor(ambient.actor):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://fixture"
            ) as client:
                response = await client.post(
                    "/a2a",
                    headers={}
                    if missing
                    else {
                        "Authorization": "Bearer fixture-verified"
                    },  # sanitizer:ignore - synthetic ASGI credential
                    json={
                        "jsonrpc": "2.0",
                        "id": "unary",
                        "method": method,
                        "params": params,
                    },
                )
            # Check within the same task, before asyncio.run restores its context.
            assert current_session() is ambient
            assert current_actor() is ambient.actor
            return response

    return asyncio.run(execute())


@pytest.mark.parametrize("failure,status", [("", 200), ("authority", 400)])
def test_default_registered_unary_binds_exact_session_and_restores_context(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    status: int,
) -> None:
    app, authority, observed, verified = registered_unary(monkeypatch, failure)
    response = unary_request(app, "tasks/get", {"id": authority.task.id})
    assert response.status_code == status
    assert len(observed) == len(verified) == 1
    bound, actor = observed[0]
    assert bound.actor is actor is verified[0]
    assert bound.scopes == frozenset({"kg:read", "kg:write"})
    assert bound.policy_version == "policy:fixture"


@pytest.mark.parametrize(
    "failure,missing,status",
    [
        ("credential", False, 401),
        ("expired", False, 401),
        ("scope", False, 403),
        ("", True, 401),
    ],
)
def test_default_registered_unary_denies_before_authority_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    missing: bool,
    status: int,
) -> None:
    app, authority, observed, _verified = registered_unary(monkeypatch, failure)
    response = unary_request(
        app, "tasks/cancel", {"id": authority.task.id}, missing=missing
    )
    assert response.status_code == status
    assert observed == []
    assert authority.task.status.state == "submitted"


@pytest.mark.parametrize(
    "failure",
    [
        "revoked",
        "tenant",
        "principal",
        "scopes",
        "policy_version",
        "missing-session",
        "session-mismatch",
    ],
)
def test_unary_refresh_refuses_changed_authority_before_dispatch(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    app, authority, observed, _verified = registered_unary(monkeypatch)
    original = AmbientA2AAuthenticator.authenticate

    async def authenticate(self: Any, request: Any, *, scope: str) -> Any:
        caller = await original(self, request, scope=scope)
        if failure == "missing-session":
            return replace(caller, session=None)
        if failure == "session-mismatch":
            return replace(caller, principal="person:changed")
        original_claims = type(caller.session).engine_verified_context

        def changed_claims(bound: Any) -> Any:
            if failure == "revoked":
                raise PermissionError("fixture revocation detail")
            claims = dict(original_claims(bound))
            claims[failure] = [] if failure == "scopes" else "changed"
            return claims

        monkeypatch.setattr(
            type(caller.session), "engine_verified_context", changed_claims
        )
        return caller

    monkeypatch.setattr(AmbientA2AAuthenticator, "authenticate", authenticate)
    response = unary_request(app, "tasks/cancel", {"id": authority.task.id})
    assert response.status_code in {401, 403}
    assert response.json()["error"]["message"] == "A2A authority refused"
    assert observed == []
    assert authority.task.status.state == "submitted"


def test_default_registered_plan_confirm_stays_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _authority, observed, verified = registered_unary(monkeypatch)
    response = unary_request(
        app,
        "graphos.plan/confirm",
        {
            "taskId": "task:one",
            "pendingCallId": "pending:one",
            "planRef": "plan:one",
            "signature": "fixture-signature",
        },
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == -32011
    assert len(verified) == 1 and observed == []


@pytest.mark.parametrize("mismatch", ["actor", "tenant"])
def test_minter_cannot_substitute_verified_actor_or_tenant(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    app, runtime, audit, journal, _requests, _verified = fixture(monkeypatch)
    real_mint = request_identity.mint_graph_session

    def substitute(actor: ActorContext) -> Any:
        if mismatch == "actor":
            return real_mint(replace(actor, actor_id="person:substituted"))
        # An invalid owner response cannot become a caller, even before the
        # GraphSession constructor's own tenant-binding check could apply.
        return SimpleNamespace(actor=actor, tenant="tenant:substituted")

    monkeypatch.setattr(request_identity, "mint_graph_session", substitute)
    response = post(app)
    assert response.status_code == 401
    assert_no_effect(runtime, audit, journal)


@pytest.mark.parametrize("failure", ["verifier-http500", "verifier-config"])
def test_verifier_outage_is_not_misreported_as_a_credential_denial(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    app, runtime, audit, journal, _requests, _verified = fixture(
        monkeypatch, failure=failure
    )
    if failure == "verifier-http500":
        response = post(app)
        assert response.status_code == 500
    else:
        with pytest.raises(
            RuntimeError, match="fixture verifier configuration unavailable"
        ):
            post(app)
    assert_no_effect(runtime, audit, journal)
