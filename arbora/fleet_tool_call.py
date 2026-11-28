"""Call one fleet MCP tool once and decode its result.

A caller names the server by ``url``, or by ``server`` as an entry of the
configured ``mcp_config.json`` (``MCP_CONFIG``, else the connector config
directory). ``action`` and ``params`` follow the fleet convention: the action
goes in ``action`` and the parameters go JSON-encoded in ``params_json``, or
inline with ``params_style="args"``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from arbora.core.environment import read_env
from arbora.security.error_surface import carry_errors

__all__ = ["FleetToolCallError", "call_fleet_tool", "run_coroutine"]


class FleetToolCallError(RuntimeError):
    """The fleet server is not configured or the tool call failed."""


def _server_entry(server: str) -> dict[str, Any]:
    from scion_sdk.paths import get_mcp_config_path

    path = str(read_env("MCP_CONFIG", "") or "").strip() or get_mcp_config_path()
    if not path:
        raise FleetToolCallError("no MCP configuration is available")
    servers = json.loads(Path(path).read_text("utf-8")).get("mcpServers", {})
    entry = servers.get(server)
    if not isinstance(entry, dict):
        raise FleetToolCallError("the fleet server is not configured")
    return entry


def _arguments(action: str, params: dict[str, Any], params_style: str) -> dict:
    arguments: dict[str, Any] = {"action": action} if action else {}
    if params_style == "json":
        arguments["params_json"] = json.dumps(params, default=str)
    else:
        arguments.update(params)
    return arguments


async def call_fleet_tool(
    *,
    tool: str,
    url: str = "",
    server: str = "",
    action: str = "",
    params: dict[str, Any] | None = None,
    params_style: str = "json",
    timeout: float = 60.0,
    client: Any = None,
) -> Any:
    """Call ``tool`` once on a fleet server and return the decoded result.

    ``client`` replaces the FastMCP client (tests). The call fails with
    :class:`FleetToolCallError` and never echoes the arguments.
    """
    if client is None:
        from fastmcp import Client

        target: Any = url or {"mcpServers": {server: _server_entry(server)}}
        # release-gate: covered by arbora.fleet_tool_call._gated_call
        client = Client(target, timeout=timeout)
    arguments = _arguments(action, dict(params or {}), params_style)
    return await _gated_call(client, tool, arguments, server or "fleet", timeout)


async def _gated_call(
    client: Any, tool: str, arguments: dict[str, Any], label: str, timeout: float
) -> Any:
    """Open ``client``, pass the one release gate, then call ``tool``."""
    from scion_sdk.runner.errors import ReleaseVerificationError
    from scion_sdk.transports.mcp_session import decode_tool_result

    from arbora.fleet.release_gate import admit_release

    try:
        async with asyncio.timeout(timeout), client as open_client:
            # The one release gate, before the tool call (ARBORA-HOST-R037.3).
            await admit_release(open_client, label)
            return decode_tool_result(await open_client.call_tool(tool, arguments))
    except ReleaseVerificationError:
        raise  # the typed release refusal keeps its code
    except Exception as exc:
        raise FleetToolCallError(
            f"fleet tool call failed ({type(exc).__name__})"
        ) from exc


def run_coroutine[T](
    coro: Coroutine[Any, Any, T], *, timeout: float | None = None
) -> T:
    """Run ``coro`` to completion from sync code, inside or outside a loop.

    Inside a running loop the coroutine runs on a fresh loop in a worker
    thread. ``timeout`` bounds the caller's wait; the worker is not joined.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        if timeout is None:
            return asyncio.run(coro)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(carry_errors(asyncio.run), coro).result(timeout=timeout)
    finally:
        pool.shutdown(wait=False)
