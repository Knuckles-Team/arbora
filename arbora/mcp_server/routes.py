"""REST projection for the clean-cut Arbora action manifest."""

from __future__ import annotations

from typing import Any

from arbora.mcp_server import runtime


def _route(app: Any, prefix: str, path: str, handler: Any) -> None:
    app.add_route(prefix + path, handler, methods=["POST"])


def mount_rest_routes(app: Any, prefix: str = "") -> None:
    """Mount one REST twin per operation arbora serves.

    ``runtime.ACTION_TOOL_ROUTES`` is the only REST surface: each entry is a
    registered operation, posted as its action-routed JSON body. The retired
    per-action granular routes are not mounted.
    """

    for tool_name, path in runtime.ACTION_TOOL_ROUTES.items():
        _route(app, prefix, path, runtime._make_tool_endpoint(tool_name))


__all__ = ["mount_rest_routes"]
