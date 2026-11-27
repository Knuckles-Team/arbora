"""Compose the operation-invoke API that the served ``arbora`` process mounts.

The served process exposes ``POST <prefix>/v1/ops/<op_id>`` and the hosted
A2A operation bridge through one :class:`HostedInvocation`. This module joins
the parts that already exist:

* the generated operation registry (``registry_factory.get_registry``);
* the caller and service EG client factories and the bound operation runtime
  (``arbora.api.invoke.bootstrap``) over one :class:`EpistemicClientPool`;
* the EG control-lease plan store;
* the policy gate in the configured Eunomia mode;
* a provider input check for engine-bound operations (the engine validates
  the request shape itself);
* the fleet-call effect of the governed fleet gateway, when the served root
  supplies one.

The process authority is the session the serving lifecycle mints. It is read
at call time, so a renewed process credential applies without a rebuild.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine, Mapping
from functools import partial
from typing import Any

from arbora.access.action_policy import ActionPolicies
from arbora.core.environment import read_env
from arbora.core.settings import RuntimeConfig
from arbora.identity.ports import BrowserBoundaryFactory
from arbora.mcp_server.served_background import (
    _background_services,
    _chained_startup,
    _identity_port,
    _scim_ports,
    _scm_ingress,
    _with_loop_worker,
)
from arbora.mcp_server.served_engine import no_process_session
from arbora.mcp_server.served_finance import (
    _delegated_subject_check,
    _with_city_ports,
    _with_finance_change_ports,
    _with_finance_import_ports,
    _with_finance_read_port,
    _with_finance_schedule_host,
    _with_market_compute_port,
)
from arbora.mcp_server.served_schema import (
    _deployment_library_startup,
    _rank_schema_lookup,
    _with_task_plan_schema,
    validate_provider_schema,
)
from arbora.security.key_cache import JwksCache

__all__ = [
    "PoolLeases",
    "refused_fleet_effect",
    "compose_served_invocation",
    "served_invocation_from_settings",
    "validate_provider_schema",
]

#: The lease kind of console plans in the EG control-lease allowlist.
PLAN_LEASE_KIND = "arbora.plan"


class PoolLeases:
    """``control_leases`` of the service-authorized EG client, resolved per call."""

    def __init__(self, service_client: Callable[[], Any]) -> None:
        self._service_client = service_client

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            client = await self._service_client()
            return await getattr(client.control_leases, name)(*args, **kwargs)

        return call


async def refused_fleet_effect(
    _op: Any, params: Mapping[str, Any], _caller: Any
) -> Any:
    """Refuse ``fleet.call`` until the served root composes the governed fleet ops.

    The governed gateway (``arbora.fleet.gateway_ops.compose_fleet_gateway``)
    needs the multiplexer operation service, which the served root does not
    compose yet; ``fleet.call`` fails closed instead of calling ungoverned.
    """
    from arbora.api.invoke.pipeline import OperationRefused

    del params
    raise OperationRefused(
        "UNAVAILABLE", {"reason": "governed fleet calls unavailable"}
    )


def _with_ingest_runner(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
) -> Mapping[str, Any] | None:
    """Bind the connector-source ``ingest_runner`` over the fleet gateway.

    ARBORA-OPS-R020.6.3: the runner reaches connectors through the governed
    ``fleet_gateway`` binding and the engine through the served pool.
    """
    if not bindings or "fleet_gateway" not in bindings or "ingest_runner" in bindings:
        return bindings
    from arbora.ingest.connector_sources import ConnectorIngestRunner
    from arbora.semantic_provisioning import CONTROL_GRAPH

    async def data_client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    async def control_client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), CONTROL_GRAPH)

    runner = ConnectorIngestRunner(
        fleet=bindings["fleet_gateway"],
        data_client=data_client,
        control_client=control_client,
    )
    return {**bindings, "ingest_runner": runner}


def _with_a2a_peer_transport(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the hub-federation peer transport (ARBORA-A2A-R024).

    A peer is reached through the one SDK A2A client; its credential resolves per
    forward from the registry reference and is never held here.
    """
    if bindings is None or "a2a_peer_transport" in bindings:
        return bindings
    from arbora.a2a.peer_transport import A2APeerTransport

    return {**bindings, "a2a_peer_transport": A2APeerTransport()}


def _with_meeting_summarizer(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the meeting ports: the fleet summarizer, policy admission and engine reads (ARBORA-OPS-R065.3)."""
    if bindings is None or "meeting_summarizer" in bindings:
        return bindings
    from arbora.api.ops.meeting_engine import (
        EngineMeetingFinal,
        EngineMeetingTranscript,
        FleetPolicyAdmission,
    )
    from arbora.api.ops.meeting_fleet import FleetMeetingSummarizer

    ports = {
        "meeting_summarizer": FleetMeetingSummarizer(),
        "meeting_provider_admission": FleetPolicyAdmission(),
        "meeting_transcript": EngineMeetingTranscript(),
        "meeting_final": EngineMeetingFinal(),
    }
    return {**ports, **bindings}


def _with_fleet_source_executor(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    process_session: Callable[[], Any] = no_process_session,
) -> Mapping[str, Any] | None:
    """Expose the governed foreign-source reader to internal query paths.

    The binding is a factory because a source executor carries the verified
    caller for its fleet gateway calls.  It does not register or approve a
    source; those writes belong to EG's owner-scoped ConnectorPack and
    RegisterForeignSource methods.  Query code receives the same graph-routed
    ``ServedEngine`` and must supply the EG approval receipt and approved
    catalog mapping before calling ``read_approved``.
    """

    if not bindings or "fleet_gateway" not in bindings:
        return bindings
    if "fleet_source_executor" in bindings:
        return bindings
    from arbora.fleet.source_executor import FleetSourceExecutor
    from arbora.mcp_server.served_engine import ServedEngine

    engine = ServedEngine(pool, process_session)

    def source_executor_for(
        caller: Any, *, overall_timeout_s: float = 60.0
    ) -> FleetSourceExecutor:
        return FleetSourceExecutor.from_engine(
            bindings["fleet_gateway"],
            caller,
            engine,
            graph=graph,
            overall_timeout_s=overall_timeout_s,
        )

    def source_binding_for(
        caller: Any,
        source_id: str,
        *,
        overall_timeout_s: float = 60.0,
    ) -> Any:
        """Resolve one approved source in the caller's startup scope.

        Resolution is deliberately lazy: fleet onboarding binds the process
        catalog after invocation composition.  The caller's verified tenant
        and graph are required to match that catalog before a source binding
        is returned; no request field can select another tenant projection.
        """

        from arbora.api.errors import not_served
        from arbora.fleet.source_executor import FleetSourceBinding

        catalog = bindings.get("fleet_virtual_catalog")
        if catalog is None:
            raise not_served(
                "arbora.fleet.virtual_catalog", "MEDULLA-UNIFIED-DATA-PLANE-R037"
            )
        tenant = getattr(caller, "tenant", None)
        session = getattr(caller, "session", None)
        caller_graph = getattr(session, "graph", None)
        if not isinstance(tenant, str) or not isinstance(caller_graph, str):
            raise LookupError("verified caller scope is unavailable")
        source = catalog.approved_source_for(
            source_id,
            scope=(tenant, caller_graph),
        )
        return FleetSourceBinding(
            executor=source_executor_for(caller, overall_timeout_s=overall_timeout_s),
            source=source,
        )

    return {
        **bindings,
        "fleet_source_executor": source_executor_for,
        "fleet_source_binding": source_binding_for,
    }


def _with_nl_federation_planner(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Register the native, read-only NL preparation port when available.

    ``NlQueryPlan`` is generated from the Medulla/EG contract.  A process
    built without that producer artifact remains unbound and therefore cannot
    accidentally fall back to ``NlQueryExplain`` or an Arbora-side planner.
    The source binding is composed first so the later dispatcher can resolve
    the exact owner-scoped catalog entry before opening a foreign read.
    """

    if not bindings or "fleet_source_binding" not in bindings:
        return bindings
    if "nl_federation_planner" in bindings:
        return bindings
    try:
        from medulla.generated import METHOD_IDS
        from medulla.generated import query as generated_query
    except (ImportError, ModuleNotFoundError):
        return bindings
    if "NlQueryPlan" not in METHOD_IDS:
        return bindings
    send_plan = getattr(generated_query, "send_nl_query_plan", None)
    decode_plan = getattr(generated_query, "decode_nl_query_plan", None)
    if not callable(send_plan) or not callable(decode_plan):
        return bindings
    from arbora.api.ops.query_federation import NativeNlQueryPlanPlanner

    return {
        **bindings,
        "nl_federation_planner": NativeNlQueryPlanPlanner(send_plan, decode_plan),
    }


def _with_approval_notices(
    bindings: Mapping[str, Any],
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> tuple[Mapping[str, Any], Callable[[], Coroutine[Any, Any, Any]]]:
    """Bind the digest channel and the outbox consumers of the serving process.

    ``approvals.digest.send`` delivers through the governed reach notifier to
    the tenant owners (ARBORA-OPS-R918.5.1). The trust-flag notice
    (ARBORA-OPS-R855.3.1) and the approver notice (ARBORA-OPS-R918.6) run as
    one scheduled runner started by the process start-up step. Both send to the
    engine as the process service identity; a pinned engine without the outbox
    shapes refuses ``UNAVAILABLE`` inside each tick (``arbora.pin_lag.medulla``).
    """
    from arbora.api.domain.fleet.approval_notifier import digest_delivery, role_holders
    from arbora.api.domain.fleet.outbox_consumer import OutboxRunner
    from arbora.api.invoke.bootstrap import build_outbox_consumers
    from arbora.messaging.service import ReachNotifier

    async def send(method: str, params: dict[str, Any]) -> Any:
        from medulla.generated import send_by_id

        client = await pool.connected_client(
            connect_context(str(session().tenant)), graph
        )
        return await send_by_id(client, method, params, graph)

    notifier = ReachNotifier()
    runner = OutboxRunner(
        build_outbox_consumers(send, notifier),
        send,
        graph=graph,
        interval_s=float(read_env("ARBORA_OUTBOX_INTERVAL_S")),
    )

    async def startup() -> None:
        runner.start()

    bound = {
        "approval_digest_delivery": digest_delivery(role_holders(send), notifier),
        "outbox_runner": runner,
    }
    return {**bound, **bindings}, startup


def _bind_late_doors(services: Any, *doors: Any) -> None:
    """Bind each late door to the invoke services once they exist."""
    for door in filter(None, doors):
        door.bind(services)


def _with_capacity(
    bindings: Mapping[str, Any],
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> tuple[Mapping[str, Any], Callable[[], Coroutine[Any, Any, Any]]]:
    """Bind the throttle controller, its limiter and the engine verdict sender.

    ARBORA-CAPACITY-R001.4 and R751: a raise asks the engine for its verdict through the
    host's ``Decide`` sender, so the loop can raise as well as narrow.
    """
    if "throttle_registry" in bindings:

        async def nothing() -> None:
            return None

        return bindings, nothing
    from arbora.fleet.capacity_binding import with_capacity

    async def send(method: str, params: dict[str, Any], tenant: str) -> Any:
        from medulla.generated import send_by_id

        client = await pool.connected_client(connect_context(tenant), graph)
        return await send_by_id(client, method, params, graph)

    from arbora.api.task_plan_schema import published_pin
    from arbora.fleet.capacity_binding import capacity_verdict_ask

    def tenant_pin(tenant: str) -> Any:
        return published_pin(bindings.get("task_plan_schema_pins"), tenant)

    ask = capacity_verdict_ask(
        send,
        graph=graph,
        feature_schema=tenant_pin,
        principal=lambda: str(session().actor.actor_id),
    )
    return with_capacity(
        {"capacity_ask": ask, **bindings},
        send,
        aimd_section=read_env("ARBORA_CAPACITY_AIMD"),
    )


def _with_projection_reader(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the engine schema-projection reader (ARBORA-OPS-R052.3).

    The reader sends the engine list variant under the verified caller's own
    session; a process without bindings leaves it unbound and
    ``query.projections`` refuses ``UNAVAILABLE``.
    """
    if bindings is None or "projection_reader" in bindings:
        return bindings
    from arbora.api.ops.projections import EngineProjectionReader

    return {**bindings, "projection_reader": EngineProjectionReader()}


def _with_operation_ports(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the operation ports the handlers call instead of importing a subsystem (R038.1)."""
    if bindings is None:
        return bindings
    from arbora.api.ops.ports import PORTS_BINDING
    from arbora.mcp_server.operation_ports import build_operation_ports

    return {PORTS_BINDING: build_operation_ports(), **bindings}


def _with_host_config(
    bindings: Mapping[str, Any] | None, config: RuntimeConfig
) -> Mapping[str, Any] | None:
    """Bind the host's config so an operation reads it from its context services."""
    if bindings is None:
        return bindings
    return {"config": config, **bindings}


def _with_host_owned_state(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the objects the host owns for operations: the outbound A2A peer registry
    (ARBORA-A2A-R001.7) the widget last-rows cache and the heartbeat rate fence."""
    if bindings is None:
        return bindings
    from arbora.api.ops.a2a_peers import build_peer_registry
    from arbora.api.ops.ecosystem import LastRows
    from arbora.api.ops.presence import HeartbeatFence

    return {
        "a2a_peers": build_peer_registry(),
        "widget_rows": LastRows(),
        "heartbeat_fence": HeartbeatFence(),
        **bindings,
    }


def _with_delivery_ports(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the six delivery ports to the host (ARBORA-OPS-R896.9, R896.10, R896.12).

    The lane authority, the run starter, the identity links, the source access, the owner
    receipts and the code-host connector each act as the verified caller of one request.
    Without them an assignment queues and no event is stamped. A port the host already
    bound is kept.
    """
    if bindings is None:
        return bindings
    from arbora.api.ops.delivery_ports import delivery_bindings
    from arbora.mcp_server.backing.tools import engine_tools

    def lane_client(graph: str) -> Any:
        return engine_tools._client_for(graph)  # noqa: SLF001

    return {**delivery_bindings(lane_client), **bindings}


def _with_provisioning_ports(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the fleet-backed rollback dispatcher and scale actuator (ARBORA-FLEET-R024.12).

    The engine makes no connector call, so both act through one governed
    ``fleet.call`` to the container-manager connector. A port the host already bound
    is kept.
    """
    if bindings is None:
        return bindings
    from arbora.fleet.scaling.fleet_actuator import FleetScaleActuator, deploy_rollback

    return {
        "deploy_rollback": deploy_rollback,
        "scale_actuator": FleetScaleActuator(),
        **bindings,
    }


def _with_scale_ports(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the scale reconciler over engine WorkItems and the graph authority (R024.14, R024.15).

    The ledger is the engine's WorkItems (claim, lease and expiry fence the controller,
    the idempotency key holds the identity), the verdict is the engine's ``Decide``
    answer and the authority snapshot is read from graph nodes as the verified caller;
    Arbora keeps none of them. A port the host already
    bound is kept; the reconciler needs the WorkItem engine and the scale actuator.
    """
    if bindings is None:
        return bindings
    from arbora.api.domain.fleet.scaling.graph_authority import GraphScaleAuthority
    from arbora.fleet.scaling.engine_policy import EngineScalePolicy
    from arbora.fleet.scaling.scaling_intent_reconciler import ScaleIntentReconciler
    from arbora.fleet.scaling.work_item_ledger import WorkItemScaleIntentLedger

    bound = {"scale_authority": GraphScaleAuthority(), **bindings}
    engine, actuator = bound.get("task_plan_work_engine"), bound.get("scale_actuator")
    if "scale_reconciler" in bound or engine is None or actuator is None:
        return bound
    reconciler = ScaleIntentReconciler(
        WorkItemScaleIntentLedger(engine), actuator, policy=EngineScalePolicy()
    )
    return {"scale_reconciler": reconciler, **bound}


def _with_security_evaluation(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the engine security evaluation (ARBORA-OPS-R051.3).

    The engine selects the probes and judges their scores. The probe runner is the
    harness adapter the host supplies as ``security_probe_runner``; without it
    ``security.evaluate`` refuses ``UNAVAILABLE`` before any probe starts.
    """
    if bindings is None or "security_evaluation" in bindings:
        return bindings
    from arbora.api.ops.security_eval import EngineSecurityEvaluation

    return {**bindings, "security_evaluation": EngineSecurityEvaluation()}


def _with_operator_audit(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the operator-only audit chain behind pin-lag (ARBORA-OPS-R052.5).

    The adapter is always composed; it records and reads only once the pinned engine
    serves the operator chain, and ``ops.doctor`` names what is missing until then.
    """
    if bindings is None or "operator_audit" in bindings:
        return bindings
    from arbora.api.ops.operator_audit import EngineOperatorAudit

    return {**bindings, "operator_audit": EngineOperatorAudit()}


def _with_federation_sources(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the engine foreign-source catalog (ARBORA-OPS-R019.1).

    Each request binds it to the caller's own session; the engine holds the record,
    its owner and its grantees. Without bindings the operations refuse ``UNAVAILABLE``.
    """
    if bindings is None or "federation_sources" in bindings:
        return bindings
    from arbora.api.ops.federation import EngineForeignSources

    return {**bindings, "federation_sources": EngineForeignSources()}


def _with_retriever(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the engine-backed retriever and plan decision (ARBORA-OPS-R053.3).

    ``retrieval.search``, ``retrieval.freshness`` and ``retrieval.space`` share the
    one ``retriever``; each request binds it to the caller's own session. Without
    bindings the operations refuse ``UNAVAILABLE``.
    """
    if bindings is None:
        return bindings
    from arbora.api.ops.retrieval_space import EngineRetrievalPlan, EngineRetriever

    adapters = {
        "retriever": EngineRetriever(),
        "retrieval_plan": EngineRetrievalPlan(),
    }
    return {**{k: v for k, v in adapters.items() if k not in bindings}, **bindings}


def _with_host_adapters(
    bindings: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """Bind the harness adapters the host supplies (ARBORA-OPS-R051.3, R055.2).

    ``security_probe_runner`` submits ``agents.run`` through the governed step
    pipeline. ``process_terminator`` ends one process through the host's per-run
    sandbox process table, resolved when the termination is applied; with no
    table composed it refuses ``UNAVAILABLE``.
    """
    if bindings is None:
        return bindings
    from arbora.agent_host.port_backends import PROCESS_TABLE
    from arbora.api.ops.host_adapters import (
        HarnessProbeRunner,
        SandboxProcessTerminator,
    )

    adapters = {
        "security_probe_runner": HarnessProbeRunner(),
        "process_terminator": SandboxProcessTerminator(PROCESS_TABLE.current),
    }
    return {**{k: v for k, v in adapters.items() if k not in bindings}, **bindings}


def compose_served_invocation(
    *,
    keys: JwksCache,
    pool: Any,
    graph: str,
    process_session: Callable[[], Any],
    action_policies: ActionPolicies,
    policy_mode: str = "none",
    plan_seal_key: bytes | None = None,
    bindings: Mapping[str, Any] | None = None,
    fleet_effect: Any = None,
    task_plan_config: Any = None,
    browser_boundary_factory: BrowserBoundaryFactory | None = None,
    promotion_ci_tool: Mapping[str, str] | None = None,
    scim_store: Any = None,
    directory_factory: Callable[[], Any] | None = None,
    scheduler_clock: Any = None,
    config: RuntimeConfig,
) -> Any:
    """One admitted :class:`HostedInvocation` over the served EG pool."""
    from arbora.api.http.auth import (
        AmbientHTTPAuthenticator,
        verified_bearer_session_for_request,
    )
    from arbora.api.invoke.bootstrap import (
        build_invoke_services,
        build_operation_runtime,
        build_service_claims,
    )
    from arbora.api.invoke.pipeline_services import EXTERNAL_EFFECT_OPERATIONS
    from arbora.api.invoke.plan import EgPlanStore
    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.policy.eunomia import PolicyGate
    from arbora.epistemic import ClientContext
    from arbora.mcp_server.hosted_invocation import HostedInvocation

    def session() -> Any:
        current = process_session()
        if current is None:
            raise RuntimeError("the process graph authority is not minted")
        return current

    def connect_context(tenant: str) -> Any:
        current = session()
        return ClientContext.from_actor(
            current.actor,
            tenant=tenant,
            audience=current.audience,
            scopes=current.scopes,
            policy_version=current.policy_version,
        )

    minted = session()
    bindings = _with_host_config(bindings, config)
    bindings = _with_host_owned_state(bindings)
    bindings = _with_ingest_runner(bindings, pool, graph, connect_context)
    bindings = _with_fleet_source_executor(bindings, pool, graph, process_session)
    bindings = _with_nl_federation_planner(bindings)
    bindings = _with_projection_reader(bindings)
    bindings = _with_operation_ports(_with_delivery_ports(_with_city_ports(bindings)))
    bindings = _with_provisioning_ports(bindings)
    bindings = _with_operator_audit(bindings)
    bindings = _with_federation_sources(bindings)
    bindings = _with_security_evaluation(bindings)
    bindings = _with_host_adapters(bindings)
    bindings = _with_retriever(bindings)
    bindings, process_startup = _with_task_plan_schema(
        bindings, pool, graph, connect_context, session, action_policies
    )
    if bindings is not None:
        process_startup = _chained_startup(
            process_startup,
            _deployment_library_startup(
                pool, graph, connect_context, session, action_policies
            ),
        )
    if bindings is not None:
        bindings, outbox_startup = _with_approval_notices(
            bindings, pool, graph, connect_context, session
        )
        process_startup = _chained_startup(process_startup, outbox_startup)
        bindings = _with_finance_import_ports(bindings)
        bindings = _with_finance_schedule_host(
            bindings, pool, graph, connect_context, session
        )
        bindings = _with_finance_change_ports(
            bindings, pool, graph, connect_context, session
        )
        bindings, loop_startup = _with_loop_worker(
            bindings, pool, graph, connect_context, session
        )
        if loop_startup is not None:
            process_startup = _chained_startup(process_startup, loop_startup)
        bindings, capacity_startup = _with_capacity(
            bindings, pool, graph, connect_context, session
        )
        process_startup = _chained_startup(process_startup, capacity_startup)
    if (
        bindings is not None
        and task_plan_config is not None
        and "task_plan_config" not in bindings
    ):
        # Native plan publication reuses the existing verified runtime path;
        # the config is trusted composition data, never request metadata.
        bindings = {**bindings, "task_plan_config": task_plan_config}
    if bindings is not None and "task_plan_work_engine" not in bindings:
        # ``agents.plan_run`` attaches to the same engine-owned WorkItem
        # authority used by A2A.  The adapter assigns the opaque item id and
        # owns submit/claim/fence transitions; plan-run never accepts a caller
        # selected native id or synthesizes a lease.
        from arbora.mcp_server.served_engine import ServedEngine

        bindings = {
            **bindings,
            "task_plan_work_engine": ServedEngine(pool, process_session),
        }
    bindings = _with_scale_ports(bindings)
    if bindings is not None and "media_ref_store" not in bindings:
        # ARBORA-A2A-R001.5.5: uploaded image bytes live in the engine, never here.
        from arbora.api.domain.a2a.media_refs import SessionMediaRefStore
        from arbora.mcp_server.served_engine import ServedEngine as _Engine

        bindings = {
            **bindings,
            "media_ref_store": SessionMediaRefStore(
                lambda: _Engine(pool), config=config
            ),
        }
    bindings = _with_a2a_peer_transport(bindings)
    bindings = _with_finance_read_port(bindings)
    bindings = _with_market_compute_port(bindings)
    bindings = _with_meeting_summarizer(bindings)
    if bindings is not None and "identity_admin" not in bindings:
        # ARBORA-IDENTITY-R018.1: the identity operations reach the engine
        # through the one administration port of the composition root.
        from arbora.identity.ports import engine_identity_admin

        bindings = {**bindings, "identity_admin": engine_identity_admin(graph)}
    if bindings is not None and "memory_materializer" not in bindings:
        # ARBORA-OPS-R024.4.4: one materializer writes accepted memory file
        # heads to the hosted directory and reports them to the engine.
        from arbora.api.ops.memory_files import MemoryMaterializer

        bindings = {**bindings, "memory_materializer": MemoryMaterializer(config)}
    if (
        bindings is not None
        and promotion_ci_tool is not None
        and "promotion_ci_tool" not in bindings
    ):
        # ARBORA-PROMOTE: the one governed pipeline read a promotion confirms.
        bindings = {"promotion_ci_tool": dict(promotion_ci_tool), **bindings}
    step_invoke = None
    if bindings is not None and "plan_step_invoke" not in bindings:
        from arbora.api.ops.plans import LateStepInvoke

        step_invoke = LateStepInvoke()
        bindings = {**bindings, "plan_step_invoke": step_invoke}

    async def service_client() -> Any:
        return await pool.connected_client(connect_context(session().tenant), graph)

    service_claims = build_service_claims(
        lambda: session().actor,
        audience=str(minted.audience),
        policy_version=str(minted.policy_version),
    )
    runtime = build_operation_runtime(
        pool=pool,
        graph=graph,
        connect_context=connect_context,
        service_scopes=frozenset(minted.scopes),
        service_principal=str(minted.actor.actor_id),
        service_claims=service_claims,
        check_access=_delegated_subject_check(pool, graph, connect_context, session),
        bindings=bindings,
    )
    # ARBORA-IDENTITY-R011.2.4, R010.2.3.2: the SCIM routes and the directory sync job
    # make their engine ``Identity`` requests as the host service identity of this tenant.
    identity_port = _identity_port(bindings, runtime, service_claims, session)

    async def audit_client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    registry = get_registry()
    changes: dict[str, Any] = {"schema_validate": validate_provider_schema}
    if registry.get("fleet.call") is not None:
        if fleet_effect is None:
            raise ValueError("fleet.call needs the governed fleet gateway effect")
        changes["fleet_effect"] = fleet_effect
    # Every external effect, and every plan-gated one, is reserved and recorded
    # in EG before dispatch: one coordinator settles a confirmed replay.
    from arbora.api.domain.mcp_server.effect_journal import EgEffectJournal
    from arbora.semantic_provisioning import CONTROL_GRAPH

    async def control_client() -> Any:
        # EG holds WorkItems on the tenant's control graph.
        return await pool.connected_client(
            connect_context(session().tenant), CONTROL_GRAPH
        )

    changes["effects"] = EgEffectJournal(
        control_client,
        claims=lambda: connect_context(session().tenant).to_claims(),
    )
    # An alert send is an external effect too: the same journal holds it
    # at most once per idempotency key (ARBORA-MESSAGING-R008).
    # Awaited attended proposals share that owner; replay never sends a
    # second browser prompt or repeats a human-approved execution.
    # The served pin and unpin writes (ARBORA-FLEET-R059.4) share the
    # owner as well: one session load and one durable record per
    # idempotency key. A SPARQL update or graph-store write from the
    # protocol endpoint (ARBORA-OPS-R857.1) is one engine mutation batch,
    # held at most once per key by the same journal.
    changes["external_effect_operations"] = frozenset(
        op_id for op_id in EXTERNAL_EFFECT_OPERATIONS if registry.get(op_id) is not None
    )

    async def gate_send(method: str, params: dict[str, Any], tenant: str) -> Any:
        """The engine as the service identity, for the approval gate question."""
        from medulla.generated import send_by_id

        from arbora.api.ops.engine_calls import decode
        from arbora.api.ops.pin_guards import refusal
        from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

        try:
            require_methods(method)
        except PinLagUnavailable as missing:
            raise refusal(missing) from missing
        client = await pool.connected_client(connect_context(tenant), graph)
        return decode(await send_by_id(client, method, params, graph))

    # ARBORA-IDENTITY-R019.3: the resource gate asks the engine for each decision, behind
    # the capability guard (PIN-LAG); a pinned release without the method refuses.
    from arbora.access.host_ports import resource_gate_for_host

    changes.setdefault("resource_gate", resource_gate_for_host(gate_send))

    from arbora.api.task_plan_schema import published_pin
    from arbora.security.session import current_session

    def confirm_principal() -> str:
        # The engine sees the host's service identity: the record's receipt names it.
        return str(session().actor.actor_id)

    def tenant_pin(tenant: str) -> Any:
        return published_pin((bindings or {}).get("task_plan_schema_pins"), tenant)

    from arbora.api.invoke.autonomy import served_autonomy

    # ARBORA-OPS-R867: every non-read effect asks the engine autonomy verdict first.
    changes.update(
        served_autonomy(
            gate_send, feature_schema=tenant_pin, principal=confirm_principal
        )
    )
    services = build_invoke_services(
        gate_send=gate_send,
        registry=registry,
        runtime=runtime,
        plans=_LeaseClient(PoolLeases(service_client)).store(
            EgPlanStore, lease_kind=PLAN_LEASE_KIND, seal_key=plan_seal_key
        ),
        policy_gate=PolicyGate(policy_mode),
        audit_client_factory=audit_client,
        config=config,
        admission_sources=_admission_sources(bindings),
        **changes,
    )
    _bind_late_doors(services, step_invoke, (bindings or {}).get("finance_paper_door"))
    from arbora.a2a.chain_composition import compose_approval_chain
    from arbora.a2a.engine_decide import build_ask_decide
    from arbora.api.invoke.eg_audit import EgAuditAdapter

    approval_chain = compose_approval_chain(
        ask_decide=build_ask_decide(
            gate_send,
            graph=graph,
            feature_schema=tenant_pin,
            principal=confirm_principal,
        ),
        leases=_LeaseClient(PoolLeases(service_client)),
        client=service_client,
        graph=graph,
        tenant=minted.tenant,
        audit_adapter=EgAuditAdapter(audit_client),
        current_session=current_session,
    )
    from arbora.mcp_server.gate_audit import gate_inactive_startup

    # A gate the pinned engine cannot serve is on the audit record once per process.
    process_startup = _chained_startup(
        process_startup,
        gate_inactive_startup(EgAuditAdapter(audit_client), session),
    )

    if services.hooks:
        # ARBORA-OPS-R009.7: a hook runs through ``invoke`` as the host service
        # principal; the tenant is the one the host session serves.
        from arbora.api.invoke.hooks import host_hook_invoker
        from arbora.api.mcp.caller import caller_from_session

        def hook_caller(tenant: str, request_id: str) -> Any:
            caller = caller_from_session(
                session(), credential_kind="service", request_id=request_id
            )
            if caller.tenant != tenant:
                raise PermissionError("hook runs only for the host tenant")
            return caller

        services.hooks.bind(host_hook_invoker(services, hook_caller))
    health_probes: dict[str, Callable[..., Any]] = {}
    invocation = HostedInvocation(
        services=services,
        approval_chain=approval_chain,
        engine_send=gate_send,
        decision_feature_schema=tenant_pin,
        ranking_feature_schema=_rank_schema_lookup(bindings),
        decision_principal=confirm_principal,
        decision_tenant=lambda: str(session().tenant),
        decision_graph=graph,
        task_plan_config=task_plan_config,
        process_startup=process_startup,
        health_probes=health_probes,
        signed_ingress=_scm_ingress(service_client, graph),
        background_services=_background_services(
            services,
            runtime,
            graph,
            session,
            health_probes,
            identity_port=identity_port,
            directory_factory=directory_factory,
            clock=scheduler_clock,
        ),
        scim_ports=_scim_ports(identity_port, scim_store),
        decide_ask=build_ask_decide(
            gate_send,
            graph=graph,
            feature_schema=tenant_pin,
            principal=confirm_principal,
        ),
        authenticator=AmbientHTTPAuthenticator(
            session_for_request=partial(verified_bearer_session_for_request, keys)
        ),
    )

    from dataclasses import replace

    if browser_boundary_factory is None:
        if config.enable_web_ui and config.webui_auth_mode == "eg":
            raise RuntimeError("EG browser requires the process-root identity factory")
        return invocation

    def browser_broker_claims() -> Any:
        current = session()
        current.ensure_authority_current()
        return connect_context(current.tenant).to_claims()

    boundary = browser_boundary_factory(
        invocation,
        service_client,
        graph=graph,
        tenant=minted.tenant,
        broker_claims=browser_broker_claims,
    )
    return replace(invocation, browser_boundary=boundary)


class _LeaseClient:
    """The ``client`` shape ``EgPlanStore`` reads: one ``control_leases``."""

    def __init__(self, leases: PoolLeases) -> None:
        self.control_leases = leases

    def store(self, factory: Any, **kwargs: Any) -> Any:
        return factory(self, **kwargs)


def served_invocation_from_settings(
    *,
    keys: JwksCache,
    process_session: Callable[[], Any],
    engine_pool: Callable[[], Any],
    action_policies: ActionPolicies,
    bindings: Mapping[str, Any],
    fleet_effect: Any,
    browser_boundary_factory: BrowserBoundaryFactory | None = None,
    config: RuntimeConfig,
) -> Any:
    """The served invocation from the deployment settings, or ``None``.

    Without ``GRAPH_SERVICE_ENDPOINTS`` the process has no engine to bind and
    serves no operation-invoke API.
    """
    from arbora.mcp_server.task_plan import configured_task_plan

    task_plan_config = configured_task_plan(config)
    if not config.graph_service_endpoints:
        if task_plan_config is not None:
            raise ValueError("task planning requires graph service endpoints")
        return None
    # The host's one pool: startup composition reads EG through it too.
    pool = engine_pool()
    raw_key = str(read_env("ARBORA_PLAN_SEAL_KEY_HEX") or "").strip()
    return compose_served_invocation(
        keys=keys,
        pool=pool,
        # Callers are admitted only on the process authority's own graph (the
        # tenant graph minted with the session) unless a deployment pins one.
        graph=str(read_env("ARBORA_GRAPH") or "").strip()
        or str(process_session().graph),
        process_session=process_session,
        action_policies=action_policies,
        policy_mode=str(read_env("EUNOMIA_MODE") or "none"),
        plan_seal_key=bytes.fromhex(raw_key) if raw_key else None,
        bindings=bindings,
        fleet_effect=fleet_effect,
        task_plan_config=task_plan_config,
        browser_boundary_factory=browser_boundary_factory,
        promotion_ci_tool=_promotion_ci_tool(),
        config=config,
    )


def _promotion_ci_tool() -> dict[str, str] | None:
    """The configured promotion pipeline read, or ``None`` when unset."""
    server = str(read_env("ARBORA_PROMOTION_CI_SERVER") or "").strip()
    tool = str(read_env("ARBORA_PROMOTION_CI_TOOL") or "").strip()
    return {"server": server, "tool": tool} if server and tool else None


def _admission_sources(bindings: Any) -> tuple[Any, Any]:
    """The catalog-backed ``(grants, declarations)``; ``(None, None)`` with no gateway.

    Without a gateway a workflow binding fails closed (``no grant source``).
    """
    gateway = (bindings or {}).get("fleet_gateway")
    if gateway is None:
        return None, None
    from arbora.fleet.admission_sources import CatalogAdmissionSources

    sources = CatalogAdmissionSources(gateway)
    return sources.grants, sources.declarations
