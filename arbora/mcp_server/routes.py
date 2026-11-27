"""REST projection for the clean-cut Arbora action manifest."""

from __future__ import annotations

from typing import Any

from arbora.mcp_server import runtime
from arbora.mcp_server.host_caches import ToolTable


def _route(app: Any, prefix: str, path: str, handler: Any) -> None:
    app.add_route(prefix + path, handler, methods=["POST"])


def mount_rest_routes(app: Any, tools: ToolTable, prefix: str = "") -> None:
    """Mount one REST twin per operation arbora serves.

    the host tool table routes is the only REST surface: each entry is a
    registered operation, posted as its action-routed JSON body. The retired
    per-action granular routes are not mounted.
    """

    for tool_name, path in tools.routes.items():
        _route(app, prefix, path, runtime._make_tool_endpoint(tools, tool_name))


__all__ = ["mount_rest_routes"]
