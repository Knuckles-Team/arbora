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

import json
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from arbora.access.action_policy import ActionPolicies, ActionPolicy
from arbora.api.registry import EgSchemaRef
from arbora.identity.ports import BrowserBoundaryFactory

__all__ = [
    "PoolLeases",
    "refused_fleet_effect",
    "compose_served_invocation",
    "served_invocation_from_settings",
    "validate_provider_schema",
]

#: The lease kind of console plans in the EG control-lease allowlist.
PLAN_LEASE_KIND = "arbora.plan"


def validate_provider_schema(
    model: EgSchemaRef, params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Admit ``params`` as the input of the engine method ``model`` names.

    The engine's run-time method catalog carries no request schemas; the
    engine validates the request shape itself. Arbora admits a JSON object:
    a mapping with string keys whose values serialize as JSON. Anything else
    raises :class:`SchemaMismatch` naming the fields, never their values; the
    invoke pipeline maps it to ``INVALID_ARGUMENT``.
    """
    if not isinstance(model, EgSchemaRef):
        raise ValueError("invalid EG schema reference")
    if not isinstance(params, Mapping):
        raise SchemaMismatch(
            {"reason": "input is not an object", "invalid": ["$ (type)"]}
        )
    if any(not isinstance(key, str) for key in params):
        raise SchemaMismatch(
            {"reason": "input field names must be strings", "invalid": ["$ (keys)"]}
        )
    invalid = sorted(key for key, value in params.items() if not _is_json(value))
    if invalid:
        raise SchemaMismatch(
            {
                "reason": "input does not serialize as JSON",
                "invalid": [f"{key} (json)" for key in invalid][:16],
            }
        )
    return dict(params)


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


class SchemaMismatch(ValueError):
    """An input that does not match its EG schema, named by field only.

    ARBORA-OPS-R009.4: ``details`` names the invalid fields and the check each
    failed. It never carries an input value.
    """

    def __init__(self, details: Mapping[str, Any]) -> None:
        super().__init__("operation input does not match its EG schema")
        self.details = dict(details)


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
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.ingest.connector_sources import ConnectorIngestRunner

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


def _with_fleet_source_executor(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
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

    engine = ServedEngine(pool)

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


def _schema_action(session: Any, graph: str, pin: Mapping[str, Any]) -> Any:
    """The exact schema intent considered by the existing action-policy owner."""
    from arbora.api.contracts import ActionRequest
    from arbora.api.task_plan_schema import COMPONENT_VERSION, PURPOSE_ID

    return ActionRequest(
        kind=PURPOSE_ID,
        target=pin["component_id"],
        params={
            "component_kind": pin["kind"],
            "component_version": COMPONENT_VERSION,
            "schema_version": pin["schema_version"],
            "content_digest": pin["content_digest"],
            "engine_version": pin["engine_version"],
            "tenant_id": session.tenant,
            "graph": graph,
            "policy_revision": str(session.policy_version),
        },
        source="arbora",
        reason="authorize the pinned task-plan feature schema publication",
        actor_id=str(session.actor.actor_id),
    )


def _publication_session(
    session: Callable[[], Any], tenant: str, graph: str, scopes: Sequence[str]
) -> Any:
    """The verified process session, holding one of ``scopes`` for ``tenant``.

    The engine classifies each publish as an exact admin or write action. A
    service identity carrying that exact capability may use it directly; the
    engine also treats ``kg:admin`` as the aggregate grant for every admin
    action. Keep the aggregate fallback here rather than lowering publication to
    the ordinary ``kg:write`` capability, which the engine deliberately refuses.
    """
    from arbora.security.session import ScopeError, resolve_session

    verified = resolve_session(session(), graph=graph)
    for scope in scopes:
        try:
            verified.require_scope(scope)
            break
        except ScopeError:
            continue
    else:
        verified.require_scope("kg:admin")
    if verified.tenant != tenant or not str(verified.policy_version).strip():
        raise PermissionError(
            "library publication needs its verified tenant and revision"
        )
    return verified


def _schema_session(session: Callable[[], Any], tenant: str, graph: str) -> Any:
    return _publication_session(session, tenant, graph, ("admin:decision-catalog",))


async def _receipted_publish_context(
    policy: ActionPolicy,
    verify: Callable[[], Any],
    action_for: Callable[[Any], Any],
    purpose_id: str,
) -> dict[str, Any]:
    """Require a new durable receipt for ``action_for(session)``, then bind it."""
    import asyncio

    from arbora.api.contracts import PolicyDisposition, PolicyReceipt
    from arbora.api.contracts.provisioning import opaque_principal
    from arbora.mcp_server.decide_wiring import _mutation_context

    request = action_for(verify())
    digest = request.digest()
    decision = await asyncio.to_thread(policy.decide, request)
    verified = verify()
    if action_for(verified).digest() != digest:
        raise PermissionError("publication intent changed during policy decision")
    receipt = decision.receipt
    if not (
        decision.allowed
        and decision.disposition is PolicyDisposition.APPROVE
        and isinstance(receipt, PolicyReceipt)
        and receipt.authorizes_effect
        and receipt.receipt_id == decision.audit_id
        and receipt.request_digest == digest
        and decision.request.digest() == digest
    ):
        raise PermissionError("publication has no exact effect-authorizing receipt")
    context = _mutation_context(verified, purpose_id)
    # AgentComponent validates the caller-shaped mutation context before EG's
    # admitted-context binder replaces its policy and process-owner fields.
    # Use the same canonical opaque principal derivation as connector-pack
    # provisioning; the native binder still authenticates and replaces the
    # authoritative carrier, tenant, and policy values.
    opaque_caller = opaque_principal(str(verified.actor.actor_id))
    context.update(
        principal=opaque_caller,
        caller_principal=opaque_caller,
        actor_scope=opaque_caller,
    )
    return {
        **context,
        "policy_digest": f"sha256:{receipt.request_digest}",
        "policy_decision_id": receipt.receipt_id,
    }


async def _schema_publish_context(
    policy: ActionPolicy,
    session: Callable[[], Any],
    graph: str,
    tenant: str,
    pin: Mapping[str, Any],
) -> dict[str, Any]:
    """Require a new durable receipt; default policy still holds this action."""
    from arbora.api.task_plan_schema import PURPOSE_ID

    return await _receipted_publish_context(
        policy,
        lambda: _schema_session(session, tenant, graph),
        lambda verified: _schema_action(verified, graph, pin),
        PURPOSE_ID,
    )


def _task_plan_schema_publisher(
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
    action_policies: ActionPolicies,
) -> Callable[[str, Mapping[str, Any]], Coroutine[Any, Any, str]]:
    """The process-lifecycle publisher; it installs the process authority."""
    from arbora.api.task_plan_schema import engine_publish
    from arbora.mcp_server.served_engine import ServedEngine
    from arbora.security.session import resolve_session, use_session

    async def client_for(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    engine = ServedEngine(pool)

    async def context_for(tenant: str, pin: Mapping[str, Any]) -> dict[str, Any]:
        policy = action_policies.for_engine(engine)
        return await _schema_publish_context(policy, session, graph, tenant, pin)

    @asynccontextmanager
    async def bind_client(tenant: str) -> AsyncIterator[Any]:
        # AgentComponent's generated senders operate on the raw EG transport.
        # Bind the process authority for the exact tenant/graph around both the
        # current read and the publish so client_scope is restored per call
        # and concurrent tenants cannot inherit one another's claims.
        async with pool.bind(connect_context(tenant), graph) as client:
            yield client

    publisher = engine_publish(client_for, context_for, graph, bind_client=bind_client)

    async def publish(tenant: str, pin: Mapping[str, Any]) -> str:
        with use_session(session()):
            verified = resolve_session(graph=graph)
            if verified.tenant != tenant:
                raise PermissionError("schema publication cannot retarget its tenant")
            return await publisher(tenant, pin)

    return publish


def _library_action(session: Any, graph: str, tenant: str) -> Any:
    """The exact deployment-library intent considered by the action-policy owner."""
    from arbora.api.contracts import ActionRequest
    from arbora.deployment.assembly_library import PURPOSE_ID, ROLES, TARGETS

    return ActionRequest(
        kind=PURPOSE_ID,
        target=f"deployment:{tenant}",
        params={
            "targets": list(TARGETS),
            "slots": list(ROLES),
            "tenant_id": session.tenant,
            "graph": graph,
            "policy_revision": str(session.policy_version),
        },
        source="arbora",
        reason="authorize the deployment templates and tool components",
        actor_id=str(session.actor.actor_id),
    )


def _deployment_library_startup(
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
    action_policies: ActionPolicies,
) -> Callable[[], Coroutine[Any, Any, Any]]:
    """The process-lifecycle publisher of the deployment library (R017.20).

    Every write binds the process authority to the exact tenant and graph, under
    a fresh receipted mutation context. A request binding never reaches it.
    """
    from arbora.deployment import assembly_library as library
    from arbora.mcp_server.served_engine import ServedEngine
    from arbora.security.session import resolve_session, use_session

    engine = ServedEngine(pool)
    tenant = str(session().tenant)

    async def context_for(for_tenant: str, key: str) -> Mapping[str, Any]:
        policy = action_policies.for_engine(engine)
        scopes = ("agent:component-write", "agent:template-write", "agent:graph-write")
        context = await _receipted_publish_context(
            policy,
            lambda: _publication_session(session, for_tenant, graph, scopes),
            lambda verified: _library_action(verified, graph, for_tenant),
            library.PURPOSE_ID,
        )
        return {**context, "idempotency_key": key}

    async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
        from medulla.generated import storage

        sender = {
            "AgentComponent": storage.send_agent_component,
            "AgentTemplate": storage.send_agent_template,
            "AgentGraph": storage.send_agent_graph,
        }[method]
        async with pool.bind(connect_context(tenant), graph) as client:
            with use_session(session()):
                resolve_session(graph=graph)
                return await sender(client, params, graph, idempotency_key=key)

    async def startup() -> dict[str, dict[str, bool]]:
        bindings = library.load_tool_bindings()
        return await library.publish_at_startup(send, context_for, (tenant,), bindings)

    return startup


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
    from arbora.api.invoke.bootstrap import build_outbox_consumers
    from arbora.core.settings import setting
    from arbora.fleet.approval_notifier import digest_delivery, role_holders
    from arbora.fleet.outbox_consumer import OutboxRunner
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
        interval_s=float(setting("ARBORA_OUTBOX_INTERVAL_S", 15.0)),
    )

    async def startup() -> None:
        runner.start()

    bound = {
        "approval_digest_delivery": digest_delivery(role_holders(send), notifier),
        "outbox_runner": runner,
    }
    return {**bound, **bindings}, startup


def _with_loop_worker(
    bindings: Mapping[str, Any],
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> tuple[Mapping[str, Any], Callable[[], Coroutine[Any, Any, Any]] | None]:
    """Start the loop run worker when a ``loop_body`` is bound (ARBORA-OPS-R022.2.13).

    The worker claims the ``loop_run`` task that ``evolution.loops.run`` enqueued
    and runs the body through the bound port. With no body bound nothing is
    claimed and the queued run stays in the engine: the capability fails closed.
    """
    body = bindings.get("loop_body")
    if body is None:
        return bindings, None
    import socket

    from arbora.core.settings import setting
    from arbora.fleet.loop_worker import LoopWorker, LoopWorkerRunner

    async def send(method: str, params: dict[str, Any]) -> Any:
        from medulla.generated import send_by_id

        client = await pool.connected_client(
            connect_context(str(session().tenant)), graph
        )
        return await send_by_id(client, method, params, graph)

    host = socket.gethostname()
    runner = LoopWorkerRunner(
        LoopWorker(send, body, worker_id=f"arbora-loops-{host}", host_iri=host),
        interval_s=float(setting("ARBORA_LOOP_WORKER_INTERVAL_S", 5.0)),
    )

    async def startup() -> None:
        runner.start()

    return {**bindings, "loop_worker_runner": runner}, startup


def _chained_startup(
    first: Callable[[], Coroutine[Any, Any, Any]] | None,
    second: Callable[[], Coroutine[Any, Any, Any]],
) -> Callable[[], Coroutine[Any, Any, Any]]:
    """Run ``first`` then ``second`` as one process start-up step."""
    if first is None:
        return second

    async def both() -> None:
        await first()
        await second()

    return both


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


def _with_task_plan_schema(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
    action_policies: ActionPolicies,
) -> tuple[Mapping[str, Any] | None, Callable[[], Coroutine[Any, Any, Any]] | None]:
    """Bind the verified task-plan schema pin (ARBORA-RELEASE-R003.4.4).

    The committed body binds only when its digest matches the installed
    engine's export. The request bindings get the read-only pins. The returned
    start-up callable is the process-lifecycle publisher: the server lifespan
    awaits it under the process authority for every served tenant. No request
    binding can reach it, so a request never borrows the process authority.
    Until a tenant is published, ``agents.plan_run`` refuses ``UNAVAILABLE``.
    """
    if bindings is None or "task_plan_schema_pins" in bindings:
        return bindings, None
    from arbora.api.task_plan_schema import publish_at_startup, startup_pins

    pins = startup_pins()
    if pins is None:
        return bindings, None
    publish = _task_plan_schema_publisher(
        pool, graph, connect_context, session, action_policies
    )

    async def publish_served_tenants() -> dict[str, bool]:
        # Arbora serves the process tenant's graph only: the engine pool binds
        # each graph to one tenant.
        return await publish_at_startup(pins, publish, (str(session().tenant),))

    return {**bindings, "task_plan_schema_pins": pins}, publish_served_tenants


def compose_served_invocation(
    *,
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
    from arbora.api.invoke.pipeline import EXTERNAL_EFFECT_OPERATIONS
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
    bindings = _with_ingest_runner(bindings, pool, graph, connect_context)
    bindings = _with_fleet_source_executor(bindings, pool, graph)
    bindings = _with_nl_federation_planner(bindings)
    bindings = _with_projection_reader(bindings)
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
        bindings, loop_startup = _with_loop_worker(
            bindings, pool, graph, connect_context, session
        )
        if loop_startup is not None:
            process_startup = _chained_startup(process_startup, loop_startup)
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

        bindings = {**bindings, "task_plan_work_engine": ServedEngine(pool)}
    if bindings is not None and "media_ref_store" not in bindings:
        # ARBORA-A2A-R001.5.5: uploaded image bytes live in the engine, never here.
        from arbora.a2a.media_refs import SessionMediaRefStore
        from arbora.mcp_server.served_engine import ServedEngine as _Engine

        bindings = {
            **bindings,
            "media_ref_store": SessionMediaRefStore(lambda: _Engine(pool)),
        }
    if bindings is not None and "identity_admin" not in bindings:
        # ARBORA-IDENTITY-R018.1: the identity operations reach the engine
        # through the one administration port of the composition root.
        from arbora.identity.ports import engine_identity_admin

        bindings = {**bindings, "identity_admin": engine_identity_admin(graph)}
    if bindings is not None and "memory_materializer" not in bindings:
        # ARBORA-OPS-R024.4.4: one materializer writes accepted memory file
        # heads to the hosted directory and reports them to the engine.
        from arbora.api.ops.memory_files import MemoryMaterializer

        bindings = {**bindings, "memory_materializer": MemoryMaterializer()}
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

    runtime = build_operation_runtime(
        pool=pool,
        graph=graph,
        connect_context=connect_context,
        service_scopes=frozenset(minted.scopes),
        service_principal=str(minted.actor.actor_id),
        service_claims=build_service_claims(
            lambda: session().actor,
            audience=str(minted.audience),
            policy_version=str(minted.policy_version),
        ),
        bindings=bindings,
    )

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
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.mcp_server.effect_journal import EgEffectJournal

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

        from arbora.api.ops._engine import decode
        from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

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
    services = build_invoke_services(
        gate_send=gate_send,
        registry=registry,
        runtime=runtime,
        plans=_LeaseClient(PoolLeases(service_client)).store(
            EgPlanStore, lease_kind=PLAN_LEASE_KIND, seal_key=plan_seal_key
        ),
        policy_gate=PolicyGate(policy_mode),
        audit_client_factory=audit_client,
        **changes,
    )
    if step_invoke is not None:
        step_invoke.bind(services)
    from arbora.a2a.chain_composition import compose_approval_chain
    from arbora.a2a.engine_decide import build_ask_decide
    from arbora.api.invoke.eg_audit import EgAuditAdapter
    from arbora.api.task_plan_schema import published_pin
    from arbora.security.session import current_session

    def confirm_principal() -> str:
        # The engine sees the host's service identity: the record's receipt names it.
        return str(session().actor.actor_id)

    approval_chain = compose_approval_chain(
        ask_decide=build_ask_decide(
            gate_send,
            graph=graph,
            feature_schema=lambda tenant: published_pin(
                (bindings or {}).get("task_plan_schema_pins"), tenant
            ),
            principal=confirm_principal,
        ),
        leases=_LeaseClient(PoolLeases(service_client)),
        client=service_client,
        graph=graph,
        tenant=minted.tenant,
        audit_adapter=EgAuditAdapter(audit_client),
        current_session=current_session,
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
    invocation = HostedInvocation(
        services=services,
        approval_chain=approval_chain,
        engine_send=gate_send,
        task_plan_config=task_plan_config,
        process_startup=process_startup,
        background_services=_background_services(services, runtime, graph, session),
        authenticator=AmbientHTTPAuthenticator(
            session_for_request=verified_bearer_session_for_request
        ),
    )

    from dataclasses import replace

    from arbora.core.settings import runtime_config

    if browser_boundary_factory is None:
        if runtime_config.enable_web_ui and runtime_config.webui_auth_mode == "eg":
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


def _background_services(
    services: Any, runtime: Any, graph: str, session: Callable[[], Any]
) -> tuple[Any, ...]:
    """The host's own schedulers, each calling as the tenant's service caller.

    The curator tick is ``ARBORA_CURATOR_TICK_S`` seconds; ``0`` turns the
    scheduler off (ARBORA-OPS-R958).
    """
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.invoke.service_caller import ServiceCallers
    from arbora.api.registry import Surface
    from arbora.core.settings import setting
    from arbora.knowledge.curator_scheduler import build_curator_scheduler
    from arbora.mcp_server.background import SystemClock

    started: list[Any] = []
    reclaim = _reclaim_service(runtime, session)
    if reclaim is not None:
        started.append(reclaim)
    push = _push_service(runtime, session)
    if push is not None:
        started.append(push)
    tick_s = float(str(setting("ARBORA_CURATOR_TICK_S", "300") or "300"))

    async def invoke_op(op_id: str, params: Any, caller: Any, key: str) -> Any:
        return await invoke(
            op_id,
            params,
            caller,
            Surface.MCP,
            services=services,
            idempotency_key=key,
        )

    callers = ServiceCallers(
        process_session=session, runtime=runtime, graph=graph, invoke=invoke_op
    )
    from arbora.agent_host.usage_ledger import LEDGER

    LEDGER.configure(callers)
    if tick_s <= 0:
        return tuple(started)
    return (build_curator_scheduler(callers, SystemClock(), tick_s=tick_s), *started)


def _reclaim_service(
    runtime: Any,
    session: Callable[[], Any],
    *,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Any] | None = None,
) -> Any:
    """The A2A run reclaim consumer for this host (ARBORA-A2A-R001.2.2), or ``None``.

    It starts with the process so a run whose host died is reclaimed by a replica; it is
    absent only when the host composes no A2A service or
    ``ARBORA_A2A_RECLAIM_POLL_S`` is ``0``.
    """
    import logging
    import time

    from arbora.a2a.run_reclaim import (
        DEFAULT_POLL_INTERVAL_S,
        build_run_reclaim_service,
    )
    from arbora.core.settings import setting

    a2a = getattr(runtime, "bindings", {}).get("a2a")
    authority = getattr(a2a, "authority", None)
    poll = float(
        str(setting("ARBORA_A2A_RECLAIM_POLL_S", str(DEFAULT_POLL_INTERVAL_S)) or "")
        or DEFAULT_POLL_INTERVAL_S
    )
    if authority is None or not hasattr(authority, "work_engine") or poll <= 0:
        return None
    log = logging.getLogger("arbora.a2a.reclaim")
    return build_run_reclaim_service(
        authority=authority,
        process_session=session,
        clock=clock or time.time,
        **({"sleep": sleep} if sleep is not None else {}),
        emit=lambda event, fields: log.warning("%s %s", event, dict(fields)),
        poll_interval_s=poll,
    )


def _push_service(
    runtime: Any,
    session: Callable[[], Any],
    *,
    sleep: Callable[[float], Any] | None = None,
) -> Any:
    """The A2A push delivery service for this host (ARBORA-A2A-R013.2), or ``None``.

    It starts with the process so a push item survives a restart; it is absent
    only when the host composes no push worker or ``A2A_PUSH_POLL_S`` is ``0``.
    """
    import logging

    from arbora.a2a.push import DEFAULT_POLL_INTERVAL_S, PushDeliveryService
    from arbora.core.settings import setting

    a2a = getattr(runtime, "bindings", {}).get("a2a")
    worker = getattr(getattr(a2a, "authority", None), "push", None)
    poll = float(
        str(setting("A2A_PUSH_POLL_S", str(DEFAULT_POLL_INTERVAL_S)) or "")
        or DEFAULT_POLL_INTERVAL_S
    )
    if worker is None or poll <= 0:
        return None
    log = logging.getLogger("arbora.a2a.push")
    return PushDeliveryService(
        worker,
        session,
        **({"sleep": sleep} if sleep is not None else {}),
        emit=lambda event, fields: log.warning("%s %s", event, dict(fields)),
        poll_interval_s=poll,
    )


class _LeaseClient:
    """The ``client`` shape ``EgPlanStore`` reads: one ``control_leases``."""

    def __init__(self, leases: PoolLeases) -> None:
        self.control_leases = leases

    def store(self, factory: Any, **kwargs: Any) -> Any:
        return factory(self, **kwargs)


def served_invocation_from_settings(
    *,
    process_session: Callable[[], Any],
    action_policies: ActionPolicies,
    bindings: Mapping[str, Any],
    fleet_effect: Any,
    browser_boundary_factory: BrowserBoundaryFactory | None = None,
) -> Any:
    """The served invocation from the deployment settings, or ``None``.

    Without ``GRAPH_SERVICE_ENDPOINTS`` the process has no engine to bind and
    serves no operation-invoke API.
    """
    from arbora.core.settings import runtime_config, setting
    from arbora.mcp_server.served_engine import process_engine_pool
    from arbora.mcp_server.task_plan import configured_task_plan

    task_plan_config = configured_task_plan()
    if not runtime_config.graph_service_endpoints:
        if task_plan_config is not None:
            raise ValueError("task planning requires graph service endpoints")
        return None
    # The one process pool: startup composition reads EG through it too.
    pool = process_engine_pool()
    raw_key = str(setting("ARBORA_PLAN_SEAL_KEY_HEX", "") or "").strip()
    return compose_served_invocation(
        pool=pool,
        # Callers are admitted only on the process authority's own graph (the
        # tenant graph minted with the session) unless a deployment pins one.
        graph=str(setting("ARBORA_GRAPH", "") or "").strip()
        or str(process_session().graph),
        process_session=process_session,
        action_policies=action_policies,
        policy_mode=str(setting("EUNOMIA_MODE", "none") or "none"),
        plan_seal_key=bytes.fromhex(raw_key) if raw_key else None,
        bindings=bindings,
        fleet_effect=fleet_effect,
        task_plan_config=task_plan_config,
        browser_boundary_factory=browser_boundary_factory,
        promotion_ci_tool=_promotion_ci_tool(setting),
    )


def _promotion_ci_tool(setting: Callable[..., Any]) -> dict[str, str] | None:
    """The configured promotion pipeline read, or ``None`` when unset."""
    server = str(setting("ARBORA_PROMOTION_CI_SERVER", "") or "").strip()
    tool = str(setting("ARBORA_PROMOTION_CI_TOOL", "") or "").strip()
    return {"server": server, "tool": tool} if server and tool else None
