"""The host's owned run-time state (ARBORA-HOST-R039).

``HostContext`` is built once by the composition root and passed by argument,
narrowest field first. A field is one owned object that holds its own lock and
is never reassigned. There is deliberately no module-level accessor: that
would be the global the context replaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arbora.a2a.composition import build_a2a_service
from arbora.a2a.engine_decide import HostDecide
from arbora.a2a.service import A2AService
from arbora.access.approval import ApprovalDecisions, verified_approval_session
from arbora.access.policy_state import PolicyState
from arbora.agent_host.host_port_adapters import HostPortsSlot
from arbora.browser_control.browser_state import BrowserState
from arbora.fleet.state import FleetState
from arbora.gateway.state import GatewayState
from arbora.mcp_server.host_caches import HostCaches, ToolTable
from arbora.mcp_server.served_engine import EngineHandles
from arbora.observability.metric_evidence import EngineMetricEvidence
from arbora.observability.state import (
    GatewayTrend,
    Metrics,
    Observability,
    RuntimeSignalBuffer,
    Signals,
    Telemetry,
    build_health_state,
)
from arbora.run_origin import RunOriginRegistry
from arbora.security.process_authority import ProcessAuthority

__all__ = [
    "HostContext",
    "RunState",
    "bind_a2a_decision",
    "bind_invocation_probes",
    "bind_ranking",
    "build_host_context",
    "compose_host_context",
    "compose_host_context_over",
]


@dataclass(frozen=True, slots=True)
class RunState:
    """Live agent runs: their origin registry and the A2A service over them."""

    origins: RunOriginRegistry
    a2a: A2AService
    #: The A2A door's engine ``Decide`` port; the served invocation binds it.
    decide: HostDecide
    #: The one ``HostPorts`` record every run entry point passes to the harness.
    ports: HostPortsSlot


@dataclass(frozen=True, slots=True)
class HostContext:
    """Every owned run-time object of one host process."""

    runs: RunState
    fleet: FleetState
    policy: PolicyState
    engine: EngineHandles
    caches: HostCaches
    browser: BrowserState
    authority: ProcessAuthority
    observability: Observability
    gateway: GatewayState


def build_host_context() -> HostContext:
    """Build the host state once; the engine pool is built on first use."""
    authority = ProcessAuthority()
    return compose_host_context_over(
        EngineHandles(process_session=authority.session),
        HostCaches(tools=ToolTable(authority)),
    )


def compose_host_context(engine: EngineHandles) -> HostContext:
    """Compose the host state over the given engine handles and fresh caches."""
    return compose_host_context_over(engine, HostCaches())


def compose_host_context_over(engine: EngineHandles, caches: HostCaches) -> HostContext:
    """Compose the host state over the given engine handles and caches.

    ``ensure_tools_registered`` composes over a cache field its caller owns; every
    other build goes through :func:`compose_host_context` and gets a fresh one.
    """
    origins = RunOriginRegistry()
    decide = HostDecide()
    ports = HostPortsSlot()
    a2a = build_a2a_service(
        engine_provider=engine.served,
        origins=origins,
        decide=decide,
        feature_schema=decide.feature_schema,
        decision_principal=decide.principal,
        ports=ports,
    )
    fleet = FleetState()
    return HostContext(
        runs=RunState(origins=origins, a2a=a2a, decide=decide, ports=ports),
        fleet=fleet,
        policy=PolicyState(
            approvals=ApprovalDecisions(verified_approval_session),
        ),
        engine=engine,
        caches=caches,
        browser=BrowserState(),
        authority=caches.tools.authority,
        observability=_compose_observability(engine, caches.tools.authority, fleet),
        gateway=GatewayState(),
    )


def _compose_observability(
    engine: EngineHandles, authority: ProcessAuthority, fleet: FleetState
) -> Observability:
    """The observability field: the fleet's child gauges are sampled at each scrape and
    an anomalous gateway latency window is kept as engine evidence."""
    metrics = Metrics()
    metrics.register(fleet.multiplexers.sample_child_health_gauges)
    trend = GatewayTrend()
    trend.bind_store(EngineMetricEvidence(engine.served, authority.session))
    return Observability(
        telemetry=Telemetry(),
        health=build_health_state(),
        signals=Signals(runtime=RuntimeSignalBuffer(), gateway=trend),
        metrics=metrics,
    )


def bind_a2a_decision(host: HostContext, invocation: Any) -> None:
    """Bind the A2A door's ``Decide`` port to the served invocation's engine sender.

    A host with no engine endpoint has no invocation, so the port stays unbound and
    a named-agent or budgeted message refuses closed (ARBORA-A2A-R023, R017.5).
    """
    send = getattr(invocation, "engine_send", None)
    schema = getattr(invocation, "decision_feature_schema", None)
    principal = getattr(invocation, "decision_principal", None)
    if send is None or schema is None or principal is None:
        return
    host.runs.decide.bind(send, feature_schema=schema, principal=principal)


def bind_invocation_probes(host: HostContext, invocation: Any) -> None:
    """Bind the readiness probes the served composition built into the host's health."""
    for name, probe in getattr(invocation, "health_probes", {}).items():
        host.observability.health.probes.bind(name, probe)


def bind_ranking(host: HostContext, invocation: Any) -> None:
    """Bind the fleet's ranking port to the served invocation's engine sender.

    A host with no engine endpoint has no invocation, so the port stays unbound and
    every ranking site answers the lexical floor (ARBORA-FLEET-R083.5).
    """
    from arbora.fleet.ranking import engine_ask, ranking_probe, schemas_published

    host.observability.health.probes.bind("ranking", ranking_probe(host.fleet.ranking))
    send = getattr(invocation, "engine_send", None)
    schema = getattr(invocation, "decision_feature_schema", None)
    principal = getattr(invocation, "decision_principal", None)
    tenant = getattr(invocation, "decision_tenant", None)
    rank_schema = getattr(invocation, "ranking_feature_schema", None)
    if send is None or schema is None or principal is None or tenant is None:
        return
    host.fleet.ranking.bind(
        engine_ask(
            send,
            tenant=tenant,
            feature_schema=schema,
            principal=principal,
            rank_schema=rank_schema,
        ),
        published=schemas_published(schema, tenant, rank_schema),
    )
