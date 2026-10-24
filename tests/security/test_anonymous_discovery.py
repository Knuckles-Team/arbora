"""Anonymous discovery and invocation share authority, never a client fallback."""

from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from arbora.api.http.app import create_api_application
from arbora.api.http.auth import (
    AmbientHTTPAuthenticator,
    verified_bearer_session_for_request,
)
from arbora.api.invoke import invoke
from arbora.api.mcp.caller import caller_from_session
from arbora.api.mcp.discovery import visible_ops
from arbora.api.mcp.resolve import IntentResolver
from arbora.api.mcp.verbs import MCPProjection, make_verb
from arbora.api.policy import PolicyGate
from arbora.api.registry import Effect, Idempotency, PrincipalRule, Surface, Verb
from arbora.api.registry.registry import authorized
from arbora.security import anonymous
from arbora.security.request_identity import ActorIdentityMiddleware
from tests.api.test_invoke_contract import operation, setup_services
from tests.security.test_anonymous_mode import local_mode as local_mode

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.3")


def read_op(**changes):
    fields = dict(
        id="example.read",
        verb=Verb.FIND,
        scopes=frozenset({"query:unified"}),
        effect=Effect.READ,
        idempotency=Idempotency.NONE,
    )
    fields.update(changes)
    return operation(**fields)


def test_discovery_validates_concrete_anonymous_authority(local_mode, monkeypatch):
    caller = caller_from_session(anonymous.session(), credential_kind="none")
    op = read_op()
    assert authorized(op, caller, policy=lambda *_: True)
    for malformed in (
        replace(caller, principal_kind="human"),
        replace(caller, authenticated=True),
        replace(caller, tenant="other"),
        SimpleNamespace(
            principal_kind="anonymous",
            effective_scopes=caller.effective_scopes,
            delegated=False,
            authenticated=True,
        ),
    ):
        assert not authorized(op, malformed, policy=lambda *_: True)
    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "external")
    assert not authorized(op, caller, policy=lambda *_: True)


@pytest.mark.parametrize("rule", list(PrincipalRule))
async def test_discovery_and_invoke_preserve_principal_rules(local_mode, rule):
    caller = caller_from_session(anonymous.session(), credential_kind="none")
    op = read_op(principals=rule)
    services, *_ = setup_services(op)
    allowed = rule is PrincipalRule.ANY
    assert authorized(op, caller, policy=lambda *_: True) is allowed
    assert (
        bool(await visible_ops(services.registry, caller, services.policy_gate))
        is allowed
    )
    result = await invoke(op.id, {}, caller, Surface.MCP, services=services)
    assert (result.code == "OK") is allowed


async def test_discovery_and_invoke_preserve_scope_narrowing(local_mode):
    session = replace(anonymous.session(), scopes=frozenset({"mcp:discover"}))
    caller = caller_from_session(session, credential_kind="none")
    op = read_op()
    services, *_ = setup_services(op)
    assert not authorized(op, caller, policy=lambda *_: True)
    assert not await visible_ops(services.registry, caller, services.policy_gate)
    result = await invoke(op.id, {}, caller, Surface.MCP, services=services)
    assert result.code == "SCOPE_REQUIRED"


@pytest.mark.parametrize("denied", [False, True])
async def test_http_mcp_discovery_and_invoke_policy_parity(local_mode, denied):
    from eunomia_core import schemas

    class Decisions:
        async def bulk_check(self, requests):
            return [schemas.CheckResponse(allowed=not denied) for _ in requests]

    op = read_op()
    gate = PolicyGate("embedded", Decisions())
    services, runtime, *_ = setup_services(op, policy_gate=gate)
    auth = AmbientHTTPAuthenticator(
        session_for_request=verified_bearer_session_for_request
    )
    app = FastAPI()
    app.mount(
        "/api/v1",
        create_api_application(
            services=services,
            visibility=gate.check_op,
            authenticator=auth,
            invoke=invoke,
        ),
    )
    transport = httpx.ASGITransport(
        app=ActorIdentityMiddleware(app), client=("127.0.0.1", 1234)
    )
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1"
    ) as client:
        registry = await client.get("/api/v1/registry")
        assert registry.status_code == 200
        ids = [item["id"] for item in registry.json()["ops"]]
        assert ids == ([] if denied else [op.id])
        descriptor = await client.get(f"/api/v1/ops/{op.id}")
        assert descriptor.status_code == (404 if denied else 200)
        document = (await client.get("/api/v1/openapi.json")).json()
        assert (f"/api/v1/ops/{op.id}" in document["paths"]) is not denied
        response = await client.post(f"/api/v1/ops/{op.id}", json={})
        assert response.json()["ok"] is not denied
    caller = caller_from_session(anonymous.session(), credential_kind="none")
    visible = await visible_ops(services.registry, caller, gate)
    assert [item.id for item in visible] == ids
    projection = MCPProjection(
        registry=services.registry,
        services=services,
        resolver=IntentResolver(),
        caller_for_request=lambda: caller,
        policy_gate=gate,
        invoke=invoke,
    )
    tool = make_verb("find", projection)
    found = (await tool.run({})).structured_content
    assert [item["op"] for item in found["result"]["items"]] == ids
    outcome = (await tool.run({"op": op.id, "params": {}})).structured_content
    assert outcome["ok"] is not denied
    assert len(runtime.calls) == (0 if denied else 2)
