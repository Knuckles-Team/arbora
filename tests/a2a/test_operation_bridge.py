"""Real A2A admission/pipeline execution with explicitly synthetic effect ports.

These tests qualify adapter wiring, not production credential verification,
engine durability, or the still-unavailable signed approval exchange.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

import httpx
import pytest
from graph_os.api.contracts.session import GraphSession, use_session
from agent_utilities.security.actor_identity import ActorType
from agent_utilities.security.brain_context import ActorContext, CredentialLease
from eunomia_core import schemas
from fastapi import FastAPI

from graph_os.a2a import mcp as a2a_mcp
from graph_os.a2a.application import create_a2a_application
from graph_os.a2a.service import A2AService
from graph_os.api.http.auth import AmbientHTTPAuthenticator
from graph_os.api.invoke.bootstrap import verify_current_from_session
from graph_os.api.policy.eunomia import PolicyGate
from graph_os.api.registry import Confirm, Surface, contract_admission
from graph_os.api.registry.eg_binding import EgContractError
from graph_os.mcp_server import runtime as mcp_runtime
from tests.a2a.test_application import Authenticator, Authority, Router
from tests.a2a.test_mcp import _Mcp
from tests.api.test_invoke_contract import FixtureRuntime, operation, setup_services

pytestmark = pytest.mark.spec("GRAPHOS-A2A-R006.2")


class SessionRuntime(FixtureRuntime):
    """Keep actual session freshness checks around the scripted effect port."""

    def __init__(self) -> None:
        super().__init__()
        self.callers: list[Any] = []
        self.change_caller = False

    async def verify_current(self, supplied: Any) -> Any:
        self.callers.append(supplied)
        current = await verify_current_from_session(supplied)
        return (
            replace(current, principal="person:substituted")
            if self.change_caller
            else current
        )


class DenyPolicy:
    async def bulk_check(self, requests: Any) -> list[Any]:
        return [
            schemas.CheckResponse(allowed=False, reason="fixture denial")
            for _ in requests
        ]


def session(scopes: frozenset[str] = frozenset({"example:write"})) -> GraphSession:
    actor = ActorContext(
        actor_id="person:one",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 3600),
    )
    return GraphSession(
        actor=actor,
        tenant=actor.tenant_id,
        scopes=scopes,
        policy_version="policy:one",
        audience="graph-os",
    )


def fixture(bound: GraphSession | None = None, **changes: Any) -> Any:
    bound = bound or session()
    services, _runtime, audit, journal, _leases = setup_services(
        changes.pop("op", None) or operation(surfaces=frozenset({Surface.A2A})),
        **changes,
    )
    runtime = SessionRuntime()
    services = replace(services, runtime=runtime)
    requests = []

    async def resolve(request: Any) -> GraphSession:
        # A fixture credential verifier: deliberately bound to this exact request.
        # Production token/issuer validation is outside this wiring qualification.
        if request.headers.get("authorization") != "Bearer fixture-verified":
            raise PermissionError("Unverified request")
        requests.append(request)
        return bound

    auth = AmbientHTTPAuthenticator(session_for_request=resolve)
    app = create_a2a_application(
        service=A2AService(authority=Authority(), router=Router()),
        authenticator=Authenticator(),
        operation_services=services,
        operation_authenticator=auth,
    )
    return app, services, runtime, audit, journal, requests


def post(
    app: Any,
    *,
    params: Any = None,
    method: str = "graphos.op/invoke",
    token: str = "fixture-verified",
) -> httpx.Response:
    async def execute() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            return await client.post(
                "/a2a",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Request-Id": "request:one",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "operation:one",
                    "method": method,
                    "params": params
                    if params is not None
                    else {
                        "op": "example.write",
                        "params": {"value": 7},
                        "idempotencyKey": "effect:one",
                    },
                },
            )

    return asyncio.run(execute())


def assert_no_effect(runtime: Any, audit: Any, journal: Any) -> None:
    assert runtime.calls == []
    assert audit.events == []
    assert journal.rows == {}


def test_real_asgi_auth_session_pipeline_and_exact_services_execute_once() -> None:
    bound = session()
    app, services, runtime, audit, journal, requests = fixture(bound)
    response = post(app)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == "operation:one"
    assert body["result"]["ok"] is True
    assert body["result"]["result"] == {"value": 7}
    assert body["result"]["meta"]["registry_digest"] == services.registry.digest
    assert len(requests) == 1
    assert len(runtime.calls) == 1
    assert runtime.calls[0][2].idempotency_key == "effect:one"
    assert all(caller.session is bound for caller in runtime.callers)
    assert all(caller.credential_kind == "bearer" for caller in runtime.callers)
    assert audit.events[0]["surface"] == Surface.A2A.value
    assert audit.events[0]["principal"] == bound.actor.actor_id
    assert audit.events[0]["tenant"] == bound.tenant
    assert len(journal.completions) == 1
    # A real retry traverses admission and the pipeline, replaying the journal.
    assert post(app).json()["result"]["result"] == {"value": 7}
    assert len(requests) == 2 and len(runtime.calls) == 1


@pytest.mark.parametrize(
    "failure", ["scope", "policy", "expired", "mismatched", "credential"]
)
def test_denied_authority_has_zero_effects(failure: str) -> None:
    bound = session(frozenset() if failure == "scope" else frozenset({"example:write"}))
    changes = (
        {"policy_gate": PolicyGate("remote", DenyPolicy())}
        if failure == "policy"
        else {}
    )
    app, _services, runtime, audit, journal, _requests = fixture(bound, **changes)
    if failure == "expired":
        assert bound.actor.credential_lease is not None
        bound.actor.credential_lease.renew(1)
    if failure == "mismatched":
        runtime.change_caller = True
    response = post(
        app,
        token="unverified"  # sanitizer:ignore - synthetic ASGI credential
        if failure == "credential"
        else "fixture-verified",
    )
    expected = {"scope": "SCOPE_REQUIRED", "policy": "POLICY_DENIED"}.get(
        failure, "UNAUTHENTICATED"
    )
    assert response.status_code in {401, 403}
    assert response.json()["error"]["data"]["error"]["code"] == expected
    assert_no_effect(runtime, audit, journal)


@pytest.mark.parametrize("part", ["services", "authenticator"])
def test_partial_composition_is_rejected(part: str) -> None:
    _app, services, _runtime, _audit, _journal, _requests = fixture()
    kwargs = (
        {"operation_services": services}
        if part == "services"
        else {"operation_authenticator": AmbientHTTPAuthenticator()}
    )
    with pytest.raises(ValueError):
        create_a2a_application(
            service=A2AService(authority=Authority(), router=Router()),
            authenticator=Authenticator(),
            **kwargs,
        )


def test_missing_composition_still_refuses_without_effects() -> None:
    app = create_a2a_application(
        service=A2AService(authority=Authority(), router=Router()),
        authenticator=Authenticator(),
    )
    response = post(
        app,
        token="verified",  # sanitizer:ignore - synthetic ASGI credential
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == -32011


@pytest.mark.parametrize(
    "method,params",
    [
        (
            "graphos.plan/confirm",
            {
                "taskId": "task:one",
                "pendingCallId": "pending:one",
                "planRef": "plan:one",
                "signature": "fixture-signature",
            },
        ),
        (
            "graphos.op/invoke",
            {
                "op": "example.write",
                "params": {},
                "planRef": "plan:one",
                "idempotencyKey": "effect:one",
            },
        ),
    ],
)
def test_unqualified_signed_approval_routes_remain_refused(
    method: str, params: Any
) -> None:
    app, _services, runtime, audit, journal, _requests = fixture()
    # The legacy task facade has its own explicit fixture credential authority.
    token = (  # sanitizer:ignore - synthetic ASGI credential
        "verified" if method == "graphos.plan/confirm" else "fixture-verified"
    )
    response = post(app, params=params, method=method, token=token)
    assert response.status_code == 501
    assert response.json()["error"]["code"] == -32011
    assert_no_effect(runtime, audit, journal)


def test_registration_contract_refusal_exposes_no_routes_or_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app, services, runtime, audit, journal, _requests = fixture()
    registrar = _Mcp()
    admitted: list[Any] = []

    def refuse(registry: Any) -> None:
        admitted.append(registry)
        raise EgContractError("fixture provider receipt mismatch")

    monkeypatch.setattr(contract_admission, "validate_contract_admission", refuse)
    monkeypatch.setattr(mcp_runtime, "REGISTERED_TOOLS", {})
    monkeypatch.setattr(
        a2a_mcp, "_service", lambda: A2AService(authority=Authority(), router=Router())
    )
    with pytest.raises(EgContractError, match="provider receipt mismatch"):
        a2a_mcp.register_a2a_tools(
            registrar,
            operation_services=services,
            operation_authenticator=AmbientHTTPAuthenticator(),
        )
    assert admitted == [services.registry]
    assert registrar.tools == {} and registrar.routes == {}
    assert mcp_runtime.REGISTERED_TOOLS == {}
    assert_no_effect(runtime, audit, journal)


def test_registered_mcp_route_reaches_request_auth_and_shared_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bound = session()
    _app, services, runtime, audit, journal, _requests = fixture(bound)
    registrar = _Mcp()
    admitted: list[Any] = []
    requests = []

    async def resolve(request: Any) -> GraphSession:
        if request.headers.get("authorization") != "Bearer fixture-verified":
            raise PermissionError("Unverified request")
        requests.append(request)
        return bound

    # Explicit fixture admission: this tests registration wiring, not an EG receipt.
    monkeypatch.setattr(
        contract_admission, "validate_contract_admission", admitted.append
    )
    monkeypatch.setattr(mcp_runtime, "REGISTERED_TOOLS", {})
    monkeypatch.setattr(
        a2a_mcp, "_service", lambda: A2AService(authority=Authority(), router=Router())
    )
    a2a_mcp.register_a2a_tools(
        registrar,
        operation_services=services,
        operation_authenticator=AmbientHTTPAuthenticator(session_for_request=resolve),
    )
    assert admitted == [services.registry]
    assert mcp_runtime.REGISTERED_TOOLS["graph_a2a"] is registrar.tools["graph_a2a"]
    app = FastAPI()
    for (path, methods), handler in registrar.routes.items():
        app.add_api_route(path, handler, methods=list(methods))
    response = post(app)
    assert response.status_code == 200
    assert response.json()["result"]["result"] == {"value": 7}
    assert len(requests) == 1 and len(runtime.calls) == 1
    assert all(caller.session is bound for caller in runtime.callers)
    assert audit.events[0]["surface"] == Surface.A2A.value
    assert len(journal.completions) == 1


def test_real_plan_preview_cannot_authorize_missing_signed_exchange() -> None:
    app, _services, runtime, audit, journal, _requests = fixture(
        op=operation(confirm=Confirm.PLAN, surfaces=frozenset({Surface.A2A}))
    )
    preview = post(app)
    assert preview.status_code == 428
    outcome = preview.json()["result"]["error"]
    assert outcome["code"] == "CONFIRMATION_REQUIRED"
    plan_ref = outcome["details"]["plan_ref"]
    assert isinstance(plan_ref, str) and plan_ref
    assert_no_effect(runtime, audit, journal)
    confirmed = post(
        app,
        params={
            "op": "example.write",
            "params": {"value": 7},
            "idempotencyKey": "effect:one",
            "planRef": plan_ref,
        },
    )
    assert confirmed.status_code == 501
    assert confirmed.json()["error"]["code"] == -32011
    assert_no_effect(runtime, audit, journal)


def test_ambient_session_cannot_replace_missing_request_authority() -> None:
    bound = session()
    _app, services, runtime, audit, journal, _requests = fixture(bound)
    app = create_a2a_application(
        service=A2AService(authority=Authority(), router=Router()),
        authenticator=Authenticator(),
        operation_services=services,
        operation_authenticator=AmbientHTTPAuthenticator(),
    )
    with use_session(bound):
        response = post(app)
    assert response.status_code == 401
    assert response.json()["error"]["data"]["error"]["code"] == "UNAUTHENTICATED"
    assert_no_effect(runtime, audit, journal)
