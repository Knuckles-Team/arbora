"""The bounded parse of an MCP client catalog file (``mcpServers``).

The fleet owns the catalog of MCP servers it loads; the gateway's dashboard
discovery reads the same file through this parse.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["MAX_MCP_CATALOG_BYTES", "MAX_MCP_SERVERS", "parse_mcp_servers"]

MAX_MCP_CATALOG_BYTES = 4 * 1024 * 1024
MAX_MCP_SERVERS = 512


def parse_mcp_servers(raw: bytes) -> dict[Any, Any]:
    """Parse and validate the bounded MCP server catalog payload."""
    if len(raw) > MAX_MCP_CATALOG_BYTES:
        raise ValueError("MCP catalog exceeds its size boundary")
    mcp_config = json.loads(raw.decode("utf-8"))
    if not isinstance(mcp_config, dict):
        raise ValueError("MCP catalog must be an object")

    servers = mcp_config.get("mcpServers", mcp_config.get("servers", {}))
    if not isinstance(servers, dict) or len(servers) > MAX_MCP_SERVERS:
        raise ValueError("MCP server catalog has an invalid shape or size")
    return servers
