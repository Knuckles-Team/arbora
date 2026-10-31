"""Native registration wiring with controlled credential/effect owners.

These tests do not qualify a deployed credential issuer or durable provider.
"""

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastmcp import FastMCP

from arbora.a2a import mcp as a2a_mcp
from arbora.a2a.service import A2AService
from arbora.api.http.auth import AmbientHTTPAuthenticator
from arbora.api.registry import Surface
from arbora.mcp_server import runtime
from arbora.mcp_server.composition import NativeGatewayApplication
from arbora.mcp_server.hosted_invocation import HostedInvocation
from tests.a2a.test_application import Authority, Router
from tests.a2a.test_operation_bridge import fixture, post, session
from tests.api.test_invoke_contract import operation

pytestmark = pytest.mark.spec("ARBORA-A2A-R006.2.2")


def composed(monkeypatch, *, scopes=frozenset({"example:write"})):
    bound = session(scopes)
    _, services, effect, audit, journal, _ = fixture(
        bound, op=operation(surfaces=frozenset({Surface.HTTP, Surface.A2A}))
    )

    async def resolve(request):
        if request.headers.get("authorization") != "Bearer fixture-verified":
            raise PermissionError("unverified fixture request")
        return bound

    admitted = []
    value = HostedInvocation(
        services, AmbientHTTPAuthenticator(session_for_request=resolve)
    )
    return value, effect, audit, journal, admitted


def mount_a2a(monkeypatch, value):
    from arbora.browser_control import mcp as browser_mcp
    from arbora.mcp_server.backing import arbora_surface

    served = FastMCP("hosted-invocation-test")
    monkeypatch.setattr(
        arbora_surface, "register_arbora_surface", lambda _mcp, **_kwargs: served
    )
    monkeypatch.setattr(browser_mcp, "register_browser_control_tools", lambda _: None)
    monkeypatch.setattr(
        a2a_mcp, "_service", lambda: A2AService(authority=Authority(), router=Router())
    )
    monkeypatch.setattr(runtime, "REGISTERED_TOOLS", {})
    runtime._register_graph_tool_surface(served, invocation=value)
    return served.http_app()


def http_post(app):
    async def request():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            return await client.post(
                "/api/v1/ops/example.write",
                headers={
                    "Authorization": "Bearer fixture-verified",
                    "Idempotency-Key": "http:one",
                },
                json={"value": 8},
            )

    return asyncio.run(request())


@pytest.mark.parametrize("allowed", [True, False])
def test_native_a2a_and_gateway_share_authority_and_effect_boundary(
    monkeypatch, allowed
):
    value, effect, audit, journal, admitted = composed(
        monkeypatch, scopes=frozenset({"example:write"}) if allowed else frozenset()
    )
    a2a = post(mount_a2a(monkeypatch, value))
    http = FastAPI()
    NativeGatewayApplication(value).mount_rest_routes(http, prefix="/api")
    response = http_post(http)
    assert a2a.status_code == response.status_code == (200 if allowed else 403)
    assert len(effect.calls) == (2 if allowed else 0)
    if allowed:
        assert {event["surface"] for event in audit.events} == {"a2a", "http"}
    else:
        assert audit.events == [] and journal.rows == {}


def test_composed_gateway_refuses_dependency_replacement(monkeypatch):
    value, *_ = composed(monkeypatch)
    app = FastAPI()
    with pytest.raises(ValueError, match="cannot replace"):
        NativeGatewayApplication(value).mount_rest_routes(
            app, prefix="/api", services=value.services
        )
    assert not any(route.path.startswith("/api") for route in app.routes)


@pytest.mark.parametrize("failure", ["credential", "expired"])
def test_native_registration_refuses_stale_or_unverified_caller(monkeypatch, failure):
    value, effect, audit, journal, _ = composed(monkeypatch)
    app = mount_a2a(monkeypatch, value)
    if failure == "expired":
        # Resolve the retained session through the actual request authenticator.
        from starlette.requests import Request

        request = Request(
            {
                "type": "http",
                "headers": [(b"authorization", b"Bearer fixture-verified")],
            }
        )
        caller = asyncio.run(value.authenticator.authenticate(request))
        caller.session.actor.credential_lease.renew(1)
    response = post(
        app,
        token="unverified"  # sanitizer:ignore - synthetic ASGI credential
        if failure == "credential"
        else "fixture-verified",
    )
    assert response.status_code == 401
    assert effect.calls == [] and audit.events == [] and journal.rows == {}


def test_native_entry_hands_same_bundle_to_both_compositions(monkeypatch):
    from arbora.core import settings as config_module
    from arbora.mcp_server import server

    value, *_ = composed(monkeypatch)
    installed = []
    monkeypatch.setattr(config_module, "load_config", lambda: None)
    monkeypatch.setattr(server, "install_gateway_application", installed.append)
    for name in (
        "_preflight_mcp_sdk_floor",
        "_configure_arbora_otel",
        "_configure_telemetry_engine_otel",
    ):
        monkeypatch.setattr(server, name, lambda: None)

    class BoundaryReached(Exception):
        pass

    def build(*, invocation):
        assert invocation is value
        raise BoundaryReached

    monkeypatch.setattr(runtime, "_build_server", build)
    monkeypatch.setenv("IS_KG_SERVER", "")
    # The entry point parses sys.argv; the test must not inherit pytest's.
    monkeypatch.setattr("sys.argv", ["arbora"])
    with pytest.raises(BoundaryReached):
        server.mcp_server(invocation=value)
    assert installed == [value]
