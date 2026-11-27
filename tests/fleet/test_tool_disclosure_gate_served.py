"""A tool outside the disclosed set refuses before the tool call (ARBORA-OPS-R866)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastmcp.exceptions import ToolError

from arbora.fleet.multiplexer import _make_forwarder
from arbora.mcp_server.registration import register_mcp_tools
from tests.api.test_mcp_shared_path import surface  # noqa: F401 - the served fixture

__all__ = ["surface"]


@pytest.mark.spec("ARBORA-OPS-R866")
def test_disclosed_tool_runs_and_undisclosed_tool_refuses_before_call(
    surface: Any,  # noqa: F811
) -> None:
    async def ranker(caller: Any, **kwargs: Any) -> list[str]:
        return [surface.tool_id]

    surface.ops.tool_ranker = ranker

    async def run() -> None:
        await register_mcp_tools(surface.mcp, surface.mux, surface.binding)
        found = await surface.ops.find_tools(surface.caller, query="read")
        assert [row["id"] for row in found["items"]] == [surface.tool_id]

        # Searching ranks the tool but does not disclose it to the session: the call
        # refuses before any child call is made.
        with pytest.raises(ToolError, match="UNKNOWN_TOOL"):
            await _make_forwarder(surface.mux, surface.native_name)()
        assert surface.calls == []

        # Loading the ranked tool discloses it; it then runs through the one path.
        await surface.ops.load(surface.caller, items=[surface.tool_id])
        native = await surface.mcp.get_tool(surface.native_name)
        result = await native.run({"x": 3})
        assert result.structured_content["result"] == {"child": "called"}
        assert [call[0] for call in surface.calls] == ["fleet.call"]

    asyncio.run(run())
