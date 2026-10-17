"""Refuse unsupported client interfaces before host side effects."""

# spec: ARBORA-HOST-R010.9

import sys
from types import ModuleType
from typing import Any
from unittest.mock import Mock

import pytest
import uvicorn

import arbora.agent_host as host


@pytest.mark.parametrize(
    "selection",
    [
        {"enable_terminal_ui": True},
        {"enable_web_ui": True},
        {"enable_web_logs": True},
        {"enable_terminal_ui": True, "enable_web_ui": True},
    ],
)
@pytest.mark.parametrize("construction", ["provided", "workspace", "tags", "default"])
def test_unavailable_interfaces_refuse_before_build_or_listen(
    monkeypatch: pytest.MonkeyPatch, selection: dict, construction: str
) -> None:
    build = Mock(side_effect=AssertionError("host app must not be built"))
    serve = Mock(side_effect=AssertionError("listener must not start"))
    monkeypatch.setattr(host, "build_agent_host_app", build)
    monkeypatch.setattr(uvicorn, "run", serve)
    runtime = ModuleType("agent_utilities.graph")
    constructors = {}
    for name in ("create_graph_agent", "initialize_graph_from_workspace"):
        constructor = Mock(
            side_effect=AssertionError("agent construction must not start")
        )
        setattr(runtime, name, constructor)
        constructors[name] = constructor
    monkeypatch.setitem(sys.modules, "agent_utilities.graph", runtime)
    construction_args: dict[str, dict[str, Any]] = {
        "provided": {"graph": object(), "graph_config": {}},
        "workspace": {"workspace": "untrusted-workspace"},
        "tags": {"tag_prompts": {"example": "text"}},
        "default": {},
    }
    args = construction_args[construction]
    with pytest.raises(host.HostInterfaceUnavailable) as raised:
        host.create_agent_server(**args, **selection)
    assert raised.value.reason == "client_unavailable"
    build.assert_not_called()
    serve.assert_not_called()
    for constructor in constructors.values():
        constructor.assert_not_called()


@pytest.mark.parametrize(
    "field", ["enable_terminal_ui", "enable_web_ui", "enable_web_logs"]
)
@pytest.mark.parametrize("value", ["false", 0, 1, {}, []])
def test_non_boolean_interface_flags_refuse_before_start(
    field: str, value: Any
) -> None:
    with pytest.raises(ValueError, match="invalid_launch"):
        host.create_agent_server(**{field: value})


@pytest.mark.parametrize(
    "selection",
    [
        {},
        {"enable_terminal_ui": False, "enable_web_ui": False, "enable_web_logs": False},
        {"enable_web_ui": None},
    ],
)
def test_default_http_host_still_reaches_existing_server(
    monkeypatch: pytest.MonkeyPatch,
    selection: dict,
) -> None:
    app = object()
    build = Mock(return_value=app)
    serve = Mock()
    monkeypatch.setattr(host, "build_agent_host_app", build)
    monkeypatch.setattr(uvicorn, "run", serve)
    host.create_agent_server(graph=object(), graph_config={}, **selection)
    build.assert_called_once()
    serve.assert_called_once_with(app, host="127.0.0.1", port=9000)
