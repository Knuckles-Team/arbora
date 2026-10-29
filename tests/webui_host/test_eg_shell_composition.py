"""Real middleware/route composition; synthetic authority, not native acceptance."""

import asyncio
from types import SimpleNamespace

import pytest

from arbora.api.http.browser_app import (
    create_browser_api_application,
    create_webui_session_boundary,
)
from arbora.identity.admission import BrowserAdmission
from arbora.identity.issuer import LocalIssuer
from tests.api._support import identity_read_op
from tests.identity.test_console_admission import CallerFixture, OwnersFixture
from tests.identity.test_modes_and_browser import ORIGIN, request_scope


@pytest.mark.parametrize("legacy_composer", [True, False])
@pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")
def test_real_composed_eg_shell_has_one_protected_route_owner(
    monkeypatch, tmp_path, legacy_composer
):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    from agent_webui import server as webui
    from agent_webui.host import config
    from fastapi.responses import JSONResponse
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from arbora.core.settings import runtime_config
    from arbora.gateway import ports
    from arbora.security.request_identity import ActorIdentityMiddleware
    from arbora.webui_host.webui_co_service import compose_web_application

    # The real loader is used. Boundary-only EG is supported; no verifier/OIDC
    # values or provider connections are needed to isolate middleware ownership.
    for name in (
        "AUTH_JWT_ISSUER",
        "WEBUI_OIDC_CLIENT_ID",
        "WEBUI_OIDC_CLIENT_SECRET",
        "WEBUI_OIDC_ISSUER",
        "WEBUI_OIDC_REDIRECT_URI",
        "WEBUI_SESSION_KEY",
        "WEBUI_OIDC_SCOPE",
        "WEBUI_OIDC_ATTENDED_ACR_VALUES",
    ):
        monkeypatch.delenv(name, raising=False)
    for name in ("auth_jwt_jwks_uri", "auth_jwt_issuer", "auth_jwt_audience"):
        monkeypatch.setattr(config, name, None, raising=False)
    monkeypatch.setattr(config, "allowed_hosts", "console.invalid", raising=False)
    monkeypatch.setattr(config, "gateway_rate_limit", 0)
    monkeypatch.setattr(runtime_config, "gateway_rate_limit", 0)
    monkeypatch.setattr(runtime_config, "gateway_metrics", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("AGENT_WEBUI_ACCESS_LOG_POLICY", "disabled")
    package = tmp_path / "agent_webui"
    dist = package / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>public-shell-fixture</body></html>")
    (dist / "assets/app.js").write_text('console.log("static-fixture")')
    monkeypatch.setattr(webui, "__file__", str(package / "server.py"))
    mounted = []

    class GatewayFixture:
        def ensure_tools_registered(self):
            mounted.append("registered")

        def mount_rest_routes(self, app, *, prefix):
            mounted.append(prefix)
            app.add_api_route(prefix + "/legacy-fixture", lambda: {"private": True})

    # Only the application port is inert; register_graph_routes and its real
    # ActorIdentityMiddleware/route registrations are deliberately not patched.
    monkeypatch.setattr(ports, "_application", GatewayFixture())

    async def scenario():
        backend = OwnersFixture()
        owner = backend.producer()
        caller = CallerFixture(backend.resolution)
        op = identity_read_op(op_id="fixture.read", http=None)
        invoked = []
        issued = []

        class Registry:
            digest = "fixture"

            def __iter__(self):
                return iter((op,))

        class FixtureIssuer(LocalIssuer):
            def __init__(self):
                pass

            async def issue(self, credential):
                issued.append(True)
                return "fixture-local-token"

        async def resolve(token, credential):
            assert token == "fixture-local-token"
            return caller

        async def visible(*args):
            return True

        async def invoke(*args, **kwargs):
            invoked.append(args)
            return {"ok": True}

        async def auth(scope, receive, send):
            await JSONResponse({"authenticated": False, "mode": "local"})(
                scope, receive, send
            )

        admission = BrowserAdmission(
            FixtureIssuer(), owner, resolve, trusted_origin=ORIGIN
        )
        operations = create_browser_api_application(
            admission=admission,
            services=SimpleNamespace(registry=Registry()),
            visibility=visible,
            invoke=invoke,
        )
        boundary = create_webui_session_boundary(
            admission=admission, auth_app=auth, operations=operations
        )
        app = webui.create_agent_web_app(
            Agent(TestModel()),
            {"get_path": lambda value: value},
            listener_host="127.0.0.1",
            auth_mode="eg",
            session_boundary=boundary,
            application_composer=compose_web_application if legacy_composer else None,
        )
        assert sum(
            m.cls is ActorIdentityMiddleware for m in app.user_middleware
        ) == int(legacy_composer)
        assert (
            any(
                getattr(route, "path", None) == "/api/legacy-fixture"
                for route in app.routes
            )
            is legacy_composer
        )
        assert boundary.operations is operations and operations.admission is admission

        async def request(path, method="GET", authenticated=False):
            import httpx

            headers = request_scope()["headers"] if authenticated else []
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url=ORIGIN + ":8443"
            ) as client:
                response = await client.request(
                    method,
                    path,
                    headers=headers,
                    json={"user_id": "fixture"} if method == "POST" else None,
                )
            return response.status_code, response.content

        for path in ("/", "/assets/app.js"):
            status, body = await request(path)
            assert status == (401 if legacy_composer else 200)
            assert (b"fixture" in body) is (not legacy_composer)
        assert (await request("/auth/session"))[0] == 200
        assert (await request("/api/v1/ops/fixture.read", "POST"))[0] == 401
        for path in (
            "/api/legacy-fixture",
            "/api/enhanced/files/private.md",
            "/run",
            "/",
        ):
            assert (await request(path, "POST"))[0] == 503
            if path != "/":
                assert (await request(path))[0] == 503
        assert issued == invoked == []
        admitted = await request("/api/v1/ops/fixture.read", "POST", True)
        assert admitted[0] == 200, admitted
        assert len(issued) == len(invoked) == 1
        assert invoked[0][2].session is caller
        assert owner._requests == {}

    asyncio.run(scenario())
    assert mounted == (["registered", "/api"] if legacy_composer else [])
