"""graph-os reads platform config through the Graph OS config dir resolver."""

from __future__ import annotations

import json

import pytest
from agent_connector_sdk import paths


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("GRAPH_OS_CONFIG_DIR", raising=False)
    monkeypatch.delenv("AGENT_UTILITIES_CONFIG_DIR", raising=False)
    monkeypatch.setattr(paths, "_legacy_warned", False)


def _write_mcp(directory, name):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {name: {"command": "x"}}})
    )


def _servers():
    from graph_os.mcp_server.backing.kg_server import _load_mcp_config_servers

    return _load_mcp_config_servers()


def test_mcp_config_env_override(monkeypatch, tmp_path):
    _write_mcp(tmp_path / "override", "override")
    monkeypatch.setenv("GRAPH_OS_CONFIG_DIR", str(tmp_path / "override"))
    assert list(_servers()) == ["override"]


def test_mcp_config_new_path_wins(tmp_path):
    _write_mcp(tmp_path / "xdg" / "graph-os", "new")
    _write_mcp(tmp_path / "xdg" / "agent-utilities", "legacy")
    assert list(_servers()) == ["new"]


def test_mcp_config_legacy_fallback_warns(monkeypatch, tmp_path):
    _write_mcp(tmp_path / "xdg" / "agent-utilities", "legacy")
    calls = []
    monkeypatch.setattr(
        paths._platform_logger, "warning", lambda msg, *args: calls.append(args)
    )
    assert list(_servers()) == ["legacy"]
    assert len(calls) == 1
    assert calls[0][1] == tmp_path / "xdg" / "graph-os"


def test_mcp_config_neither_present():
    assert _servers() is None
