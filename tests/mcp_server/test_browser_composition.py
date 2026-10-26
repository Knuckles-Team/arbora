"""Local composition fixtures only; not installed native/browser acceptance."""

from types import SimpleNamespace

import pytest

from arbora.identity.engine import IdentityUnavailable
from arbora.mcp_server.browser_composition import configured_browser_boundary

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "http://console.invalid",
        "https://console.invalid/path",
        "https://user@console.invalid",
        "https://console.invalid#fragment",
    ],
)
def test_missing_secure_deployment_prerequisites_refuse(monkeypatch, origin):
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(runtime_config, "enable_web_ui", True)
    monkeypatch.setattr(runtime_config, "webui_auth_mode", "eg")
    monkeypatch.setattr(runtime_config, "webui_origin", origin)
    with pytest.raises(IdentityUnavailable):
        configured_browser_boundary(
            object(),
            object(),
            graph="fixture-graph",
            tenant="fixture-tenant",
            broker_claims=lambda: {},
        )


def test_supervisor_receives_exact_boundary(monkeypatch):
    from arbora.mcp_server import composition

    boundary, calls = object(), []

    class Supervisor:
        def start_service(self, name, runner, session):
            runner("fixture-stop")

    monkeypatch.setattr(composition, "CoServiceSupervisor", Supervisor)
    monkeypatch.setattr(
        composition,
        "detect_composition",
        lambda *a, **k: SimpleNamespace(
            messaging_intake_configured=False,
            messaging_configured=False,
            web_ui_enabled=True,
        ),
    )
    monkeypatch.setattr(
        composition, "run_web_ui", lambda stop, **kwargs: calls.append((stop, kwargs))
    )
    composition.start_composed_services(object(), object(), session_boundary=boundary)
    assert calls == [("fixture-stop", {"session_boundary": boundary})]


@pytest.mark.parametrize(
    "scopes",
    [[], ["identity:admin"], ["kg:admin"], ["identity:*"], "identity:authenticate"],
)
def test_broker_exact_scope_is_never_implied(scopes):
    from arbora.mcp_server.browser_composition import _require_broker_claims

    with pytest.raises(PermissionError):
        _require_broker_claims({"scopes": scopes, "delegation": []})


def test_broker_scope_revocation_after_connection_await_stops_dispatch():
    import asyncio

    from arbora.mcp_server.browser_composition import _IdentityClient

    async def scenario():
        claims = {"scopes": ["identity:authenticate"], "delegation": []}

        async def connect():
            await asyncio.sleep(0)
            claims["scopes"] = ["identity:admin"]
            return object()

        with pytest.raises(PermissionError, match="identity:authenticate"):
            await _IdentityClient(connect, lambda: claims)._send("Identity")

    asyncio.run(scenario())
