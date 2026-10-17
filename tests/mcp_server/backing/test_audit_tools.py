"""Tests for the graph_audit MCP tool (G23, audit-trail closure).

CONCEPT:AU-KG.audit.hash-chain-verify

Mirrors the ``_CollectingMCP`` + ``runtime._get_engine`` monkeypatch pattern
used across the other MCP tool-surface tests (e.g. ``test_ops_causal_tools.py``)
— no live engine required.
"""

from __future__ import annotations

from graph_os.mcp_server import runtime
from graph_os.mcp_server.backing.tools.audit_tools import register_audit_tools
from tests.mcp_server.backing._support import CollectingMCP as _CollectingMCP


class _FakeGraphClient:
    def __init__(self, report=None, error=None):
        self._report = report
        self._error = error

    def audit_verify(self):
        if self._error is not None:
            raise self._error
        return dict(self._report)


class _FakeBackend:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, query, params=None):
        return list(self._rows)


class _FakeEngine:
    def __init__(self, graph=None, backend=None):
        self.graph = graph
        self.backend = backend


def _register(monkeypatch, engine):
    mcp = _CollectingMCP()
    register_audit_tools(mcp)
    monkeypatch.setattr(runtime, "_get_engine", lambda: engine)
    return mcp.tools["graph_audit"]


def test_registered_on_graphos_tool_table():
    mcp = _CollectingMCP()
    register_audit_tools(mcp)
    assert "graph_audit" in mcp.tools
    assert runtime.REGISTERED_TOOLS.get("graph_audit") is not None
    assert runtime.ACTION_TOOL_ROUTES.get("graph_audit") == "/audit"
