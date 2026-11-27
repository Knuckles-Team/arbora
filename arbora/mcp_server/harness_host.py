"""arbora registers itself as the agent harness's host at startup.

CONCEPT:ARBORA-HOST.harness.host-bindings

The harness (``meristem``) never serves tools or parses the fleet
catalog. Its ``HostPorts`` record fails closed until arbora builds these
bindings (``arbora.mcp_server.harness_ports.build_host_ports``):

* a **native toolset provider** that binds a caller-granted subset of
  arbora's own served operations for one in-process delegation, dispatched
  through the governed ``_execute_tool`` path (no self-HTTP);
* a **fleet catalog provider** mapping each fleet server arbora serves to its
  public tool prefix and client endpoint, read from the exact multiplexer
  arbora serves; and
* a **remote toolset provider** and a **tool client factory**
  (:mod:`arbora.fleet.client_toolsets`): arbora's MCP client, carrying
  its outbound service credential; and
* an **authority keepalive** factory that renews the caller's renewable
  session lease for the whole of one delegation.

This module holds the providers' bodies; the record that carries them lives on
``HostContext.runs.ports`` and no module-level registration exists.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)


def _runtime() -> Any:
    from arbora.mcp_server import runtime

    return runtime


def available_tools(tools: Mapping[str, Any]) -> tuple[str, ...]:
    runtime = _runtime()
    return tuple(sorted(tools.keys() | runtime.NATIVE_INTENT_TOOLS.keys()))


def build_toolset(
    tools: Mapping[str, Any],
    tool_names: list[str],
    toolset_id: str,
    emit: Callable[[dict[str, Any]], None],
) -> Any:
    """One in-process native toolset; ``emit`` receives each served intent call event."""
    return _runtime().build_native_arbora_toolset(
        list(tool_names), tools, toolset_id=toolset_id, emit=emit
    )


def keepalive(authority: Any) -> contextlib.AbstractAsyncContextManager[None]:
    """The authority keepalive scope of one host's process authority."""
    return _runtime().authority_keepalive_scope(authority)


def fleet_catalog_view(multiplexer: Any) -> Callable[[], Mapping[str, Any]]:
    """A provider over ``multiplexer``'s mountable catalog.

    Each entry carries the collision-free public prefix arbora serves the
    server under and its resolved (non-secret) client URL. A server whose
    entry cannot be rendered is left out rather than invented.
    """
    from meristem.host_ports import FleetServer

    def view() -> dict[str, FleetServer]:
        from arbora.fleet.multiplexer import resolve_runtime_value

        servers: dict[str, FleetServer] = {}
        for name, config in dict(multiplexer.load_catalog()).items():
            if not isinstance(name, str) or not name.strip():
                continue
            url = ""
            if isinstance(config, dict) and config.get("url"):
                try:
                    url = str(
                        resolve_runtime_value(config.get("url", ""), sensitive=False)
                    )
                except Exception as exc:  # noqa: BLE001 - omit an unresolvable URL
                    logger.debug(
                        "fleet catalog URL unresolved for %s (%s)",
                        name,
                        type(exc).__name__,
                    )
            servers[name] = FleetServer(
                prefix=multiplexer.server_prefix(name), url=url.rstrip("/")
            )
        return servers

    return view


__all__ = ["available_tools", "build_toolset", "fleet_catalog_view", "keepalive"]
