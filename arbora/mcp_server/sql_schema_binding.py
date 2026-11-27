"""The SQL catalog projection the gateway serves, bound by the composition root."""

from __future__ import annotations

from typing import Any

__all__ = ["serve_sql_schema"]


async def serve_sql_schema(schema: str | None) -> tuple[int, dict[str, Any]]:
    """Answer ``(HTTP status, JSON body)`` of the catalog projection, failing closed."""
    from arbora.mcp_server.backing.tools.graph_tools import (
        SqlSchemaUnavailable,
        sql_schema,
    )

    try:
        return 200, await sql_schema(schema=schema)
    except SqlSchemaUnavailable as exc:
        return exc.status_code, exc.as_payload()
