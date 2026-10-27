"""Local composition fixtures only; not installed native/browser acceptance."""

from types import SimpleNamespace

import pytest

from arbora.identity.browser_composition import configured_browser_boundary
from arbora.identity.engine import IdentityUnavailable

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

    monkeypatch.setenv("ENABLE_WEB_UI", "true")
    monkeypatch.setenv("WEBUI_AUTH_MODE", "eg")
    monkeypatch.setenv("WEBUI_ORIGIN", origin or "")
    with pytest.raises(IdentityUnavailable):
        configured_browser_boundary(
            object(),
            object(),
            graph="fixture-graph",
            tenant="fixture-tenant",
            broker_claims=lambda: {},
            request_dispatch=object(),
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
    from arbora.identity.browser_composition import _require_broker_claims

    with pytest.raises(PermissionError):
        _require_broker_claims({"scopes": scopes, "delegation": []})


def test_broker_scope_revocation_after_connection_await_stops_dispatch():
    import asyncio

    from arbora.identity.browser_composition import _IdentityClient

    async def scenario():
        claims = {"scopes": ["identity:authenticate"], "delegation": []}

        async def connect():
            await asyncio.sleep(0)
            claims["scopes"] = ["identity:admin"]
            return object()

        with pytest.raises(PermissionError, match="identity:authenticate"):
            await _IdentityClient(connect, lambda: claims)._send("Identity")

    asyncio.run(scenario())


def test_packaged_console_injects_exact_identity_factory(monkeypatch):
    import importlib
    import sys
    import tomllib
    from pathlib import Path

    from arbora.identity import browser_composition
    from arbora.mcp_server import server

    calls = []
    monkeypatch.setattr(sys, "argv", ["arbora", "--transport", "stdio"])
    monkeypatch.setattr(server, "mcp_server", lambda **kw: calls.append(kw))
    manifest = tomllib.loads((Path(__file__).parents[2] / "pyproject.toml").read_text())
    module, name = manifest["project"]["scripts"]["arbora"].split(":")
    getattr(importlib.import_module(module), name)()
    from arbora.mcp_server.browser_dispatch import dispatch_browser_request

    assert len(calls) == 1
    factory = calls[0]["browser_boundary_factory"]
    assert factory.func is browser_composition.configured_browser_boundary
    assert factory.keywords == {"request_dispatch": dispatch_browser_request}


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_console_static_answers_do_not_compose_identity(monkeypatch, capsys, flag):
    import sys

    from agent_connector_sdk.credentials import runtime_references

    from arbora.console import main
    from arbora.core import settings
    from arbora.mcp_server import server

    monkeypatch.setattr(
        settings, "load_config", lambda: pytest.fail("static answer loaded config")
    )
    monkeypatch.setattr(
        runtime_references,
        "resolve_runtime_secret_reference",
        lambda *a: pytest.fail("static answer resolved secret"),
    )
    monkeypatch.setattr(sys, "argv", ["arbora", flag])
    monkeypatch.setattr(
        server, "mcp_server", lambda **kw: pytest.fail("static answer entered serving")
    )
    main()
    assert "arbora" in capsys.readouterr().out


def test_direct_unconfigured_eg_server_refuses_before_serving(monkeypatch):
    import sys

    from arbora.core import settings
    from arbora.mcp_server import server

    monkeypatch.setattr(sys, "argv", ["arbora"])
    monkeypatch.setattr(settings, "load_config", lambda: None)
    monkeypatch.setenv("ENABLE_WEB_UI", "true")
    monkeypatch.setenv("WEBUI_AUTH_MODE", "eg")
    with pytest.raises(RuntimeError, match="process-root identity factory"):
        server.mcp_server()


def test_settings_composition_forwards_factory_without_constructing_it(monkeypatch):
    from arbora.mcp_server import served_engine, served_invocation, task_plan

    factory, pool, calls = object(), object(), []
    monkeypatch.setenv("GRAPH_SERVICE_ENDPOINTS", "unix:///fixture")
    monkeypatch.setattr(task_plan, "configured_task_plan", lambda: None)
    monkeypatch.setattr(served_engine, "process_engine_pool", lambda: pool)
    monkeypatch.setattr(
        served_invocation, "compose_served_invocation", lambda **kw: calls.append(kw)
    )
    served_invocation.served_invocation_from_settings(
        process_session=lambda: SimpleNamespace(graph="fixture-graph"),
        bindings={},
        fleet_effect=None,
        browser_boundary_factory=factory,
    )
    assert calls[0]["browser_boundary_factory"] is factory
    assert calls[0]["pool"] is pool
