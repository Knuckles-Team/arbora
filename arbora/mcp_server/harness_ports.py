"""The one ``HostPorts`` record arbora hands the harness (ARBORA-HOST-R040.2).

``build_host_ports`` composes every provider the served host owns into the record that
``HostContext.runs.ports`` holds and every run entry point passes to the harness. No
provider is registered in harness module state: a run carries its own record.
"""

from __future__ import annotations

from typing import Any

from arbora.mcp_server import harness_host
from arbora.mcp_server.host_context import HostContext

__all__ = ["bind_host_ports_of", "build_host_ports"]


def build_host_ports(
    host: HostContext,
    *,
    services: Any = None,
    seams: Any = None,
    multiplexer: Any = None,
) -> Any:
    """The ``HostPorts`` of the served host, or raise the ``PinLagUnavailable`` naming the gap.

    ``services`` and ``seams`` compose the engine-backed ports (decision, checkpoint,
    approval, changeset); without ``services`` only the harness-local providers bind.
    ``multiplexer`` is the served fleet multiplexer, or ``None`` when no fleet is served.
    """
    import asyncio
    from functools import partial

    from meristem.host_ports import NativeToolsetProvider

    from arbora.a2a.checkpoint import checkpoint_store
    from arbora.agent_host.harness_cycle import HostProbeProvider
    from arbora.agent_runs import decide_run, record_run_event, resolve_model
    from arbora.api.domain.agent_host.host_port_adapters import bind_host_ports
    from arbora.current_plan_publication import publication_scope
    from arbora.fleet import client_toolsets
    from arbora.pin_lag.meristem import emit_run_event

    slot = host.runs.ports

    def emit(event: dict[str, Any]) -> None:
        emit_run_event(event, slot.binding)

    def build_toolset(tool_names: list[str], toolset_id: str) -> Any:
        return harness_host.build_toolset(
            host.caches.tools, tool_names, toolset_id, emit
        )

    # The engine is resolved when a run binds, never at composition.
    store = checkpoint_store(host.engine.served)
    record_events = partial(record_run_event, engine_provider=host.engine.served)
    base: dict[str, Any] = {
        "decide": decide_run,
        "models": resolve_model,
        "scaffold_publication": publication_scope,
        "native": NativeToolsetProvider(
            available_tools=partial(harness_host.available_tools, host.caches.tools),
            build_toolset=build_toolset,
        ),
        "fleet": (
            harness_host.fleet_catalog_view(multiplexer)
            if multiplexer is not None
            else None
        ),
        "remote": client_toolsets.remote_toolset_provider(host.config),
        "tool_client": partial(client_toolsets.open_tool_client, config=host.config),
        "keepalive": partial(harness_host.keepalive, host.authority),
        "probe": HostProbeProvider(slot.current),
    }
    if services is None:
        binding = bind_host_ports(**base, checkpoint_store=store, events=record_events)
    else:
        from arbora.agent_host.host_port_composition import compose_host_ports

        binding = compose_host_ports(
            services,
            wait=asyncio.sleep,
            checkpoint_store=store,
            events=record_events,
            seams=seams,
            base=base,
        )
    if binding.refusal is not None:
        raise binding.refusal
    return binding.ports


def bind_host_ports_of(host: HostContext, **composition: Any) -> None:
    """Build the record and hold it on ``host.runs.ports``; a refusal is held, not raised."""
    from arbora.api.domain.agent_host.host_port_adapters import HostPortsBinding
    from arbora.pin_lag.meristem import PinLagUnavailable

    try:
        ports = build_host_ports(host, **composition)
    except PinLagUnavailable as refusal:
        host.runs.ports.bind(HostPortsBinding(None, refusal))
        return
    host.runs.ports.bind(HostPortsBinding(ports, None))
