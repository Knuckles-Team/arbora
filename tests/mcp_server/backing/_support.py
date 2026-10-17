"""Shared fakes for graph-os's backing-tool tests."""

from __future__ import annotations


class CollectingMCP:
    """Minimal FastMCP stand-in that captures ``@mcp.tool``-registered functions."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *, name, description="", tags=None):  # noqa: ANN001
        def _deco(fn):
            self.tools[name] = fn
            return fn

        return _deco


def register_graph_query():
    """Register the query tools onto a throwaway FastMCP and return graph_query."""
    from fastmcp import FastMCP

    from graph_os.mcp_server import runtime
    from graph_os.mcp_server.backing.tools.query_tools import register_query_tools

    register_query_tools(FastMCP("test"))
    return runtime.REGISTERED_TOOLS["graph_query"]


class FakeStdioSession:
    """A ``ClientSession`` stand-in whose ``initialize`` succeeds."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def initialize(self):
        return None
