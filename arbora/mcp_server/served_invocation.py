"""Compose the operation-invoke API that the served ``arbora`` process mounts.

The served process exposes ``POST <prefix>/v1/ops/<op_id>`` and the hosted
A2A operation bridge through one :class:`HostedInvocation`. This module joins
the parts that already exist:

* the generated operation registry (``registry_factory.get_registry``);
* the caller and service EG client factories and the bound operation runtime
  (``arbora.api.invoke.bootstrap``) over one :class:`EpistemicClientPool`;
* the EG control-lease plan store;
* the policy gate in the configured Eunomia mode;
* a provider schema validator over the EG contract schemas;
* the fleet-call effect of the governed fleet gateway, when the served root
  supplies one.

The process authority is the session the serving lifecycle mints. It is read
at call time, so a renewed process credential applies without a rebuild.
"""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path
from typing import Any

from arbora.api.registry import EgSchemaRef
from arbora.identity.ports import BrowserBoundaryFactory

__all__ = [
    "PoolLeases",
    "refused_fleet_effect",
    "compose_served_invocation",
    "engine_endpoint",
    "served_invocation_from_settings",
    "validate_provider_schema",
]

#: The lease kind of console plans in the EG control-lease allowlist.
PLAN_LEASE_KIND = "arbora.plan"


@cache
def _contract_document(filename: str) -> dict[str, Any]:
    root = Path(str(importlib.resources.files("epistemic_graph") / "contract"))
    relative = Path(filename.removeprefix("contract/"))
    if ".." in relative.parts:
        raise ValueError("invalid EG schema reference")
    document = json.loads((root / relative).read_bytes())
    if not isinstance(document, dict):
        raise ValueError("EG contract schema document is not an object")
    return document


def validate_provider_schema(
    model: EgSchemaRef, params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Validate ``params`` against the EG contract schema ``model`` names.

    Raises :class:`ValueError` for a non-conforming input; the invoke pipeline
    maps that to ``INVALID_ARGUMENT``.
    """
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    filename, separator, pointer = model.path.partition("#")
    if not separator or not filename.startswith("contract/schemas/"):
        raise ValueError("invalid EG schema reference")
    document = _contract_document(filename)
    if _is_request_envelope(document, pointer):
        # A generated method binding names the whole wire request; the
        # operation input is its ``params`` member.
        pointer = f"{pointer}/properties/params"
    elif _is_parameterless_envelope(document, pointer):
        # A method without ``params`` (``NodeCount``) takes an empty input;
        # its envelope requires only ``method``, which no caller sends.
        if params:
            raise ValueError("operation input does not match its EG schema")
        return {}
    uri = f"urn:arbora:eg-contract:{filename}"
    resource = Resource.from_contents(document, default_specification=DRAFT202012)
    validator = Draft202012Validator(
        {"$ref": f"{uri}#{pointer}"},
        registry=Registry().with_resource(uri, resource),
    )
    errors = sorted(
        validator.iter_errors(dict(params)), key=lambda error: list(error.path)
    )
    if errors:
        raise SchemaMismatch(_mismatch_details(errors)) from errors[0]
    return dict(params)


class SchemaMismatch(ValueError):
    """An input that does not match its EG schema, named by field only.

    ARBORA-OPS-R009.4: ``details`` names the unexpected, missing and invalid
    fields and the schema keyword each failed. It never carries an input value.
    """

    def __init__(self, details: Mapping[str, Any]) -> None:
        super().__init__("operation input does not match its EG schema")
        self.details = dict(details)


def _field(path: Any, name: str | None = None) -> str:
    parts = [str(part) for part in path]
    if name is not None:
        parts.append(name)
    return ".".join(parts) or "$"


def _unexpected(error: Any) -> list[str]:
    allowed = set((error.schema.get("properties") or {}).keys())
    return [
        _field(error.path, str(key)) for key in error.instance if key not in allowed
    ]


def _missing(error: Any) -> list[str]:
    return [
        _field(error.path, str(key))
        for key in error.validator_value
        if key not in error.instance
    ]


#: Schema keyword -> (details key, the field names it reports).
_NAMED_ERRORS = {
    "additionalProperties": ("unexpected", _unexpected),
    "required": ("missing", _missing),
}


def _classified(error: Any) -> tuple[str, list[str]]:
    named = _NAMED_ERRORS.get(error.validator)
    if named is not None and isinstance(error.instance, Mapping):
        key, names = named
        return key, names(error)
    return "invalid", [f"{_field(error.path)} ({error.validator})"]


def _mismatch_details(errors: list[Any]) -> dict[str, Any]:
    found: dict[str, set[str]] = {}
    for error in errors:
        key, names = _classified(error)
        found.setdefault(key, set()).update(names)
    details: dict[str, Any] = {"reason": "input does not match the operation schema"}
    for key in ("unexpected", "missing", "invalid"):
        if found.get(key):
            details[key] = sorted(found[key])[:16]
    return details


def _selected(document: Mapping[str, Any], pointer: str) -> Any:
    selected: Any = document
    for segment in pointer.lstrip("/").split("/"):
        if not isinstance(selected, Mapping) or segment not in selected:
            return None
        selected = selected[segment]
    return selected


def _is_parameterless_envelope(document: Mapping[str, Any], pointer: str) -> bool:
    selected = _selected(document, pointer)
    if not isinstance(selected, Mapping):
        return False
    properties = selected.get("properties")
    return (
        selected.get("required") == ["method"]
        and isinstance(properties, Mapping)
        and set(properties) == {"method"}
    )


def _is_request_envelope(document: Mapping[str, Any], pointer: str) -> bool:
    selected = _selected(document, pointer)
    required = selected.get("required") if isinstance(selected, Mapping) else None
    properties = selected.get("properties") if isinstance(selected, Mapping) else None
    return (
        isinstance(required, list)
        and {"method", "params"} <= set(required)
        and isinstance(properties, Mapping)
        and "params" in properties
    )


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


def engine_endpoint(endpoints: Any) -> dict[str, str]:
    """``{"socket_path": ...}`` or ``{"tcp_addr": ...}`` for the first endpoint."""
    values = [str(item).strip() for item in (endpoints or []) if str(item).strip()]
    if not values:
        raise ValueError("GRAPH_SERVICE_ENDPOINTS names no engine endpoint")
    endpoint = values[0]
    if endpoint.startswith("unix://"):
        return {"socket_path": endpoint.removeprefix("unix://")}
    if endpoint.startswith("/"):
        return {"socket_path": endpoint}
    return {"tcp_addr": endpoint.removeprefix("tcp://")}


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


def _with_ops_runners(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
) -> Mapping[str, Any] | None:
    """Bind the engine-backed ``decide_runner`` and ``work_runner``.

    ARBORA-OPS-R021.1 / R022.1: both reach Medulla through the served pool.
    WorkItems live on the control graph; decisions on the tenant graph.
    """
    if bindings is None:
        return bindings
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
    from arbora.mcp_server.ops_runners import EngineDecideRunner, EngineWorkRunner

    async def data_client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    async def control_client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), CONTROL_GRAPH)

    added: dict[str, Any] = {}
    if "decide_runner" not in bindings:
        added["decide_runner"] = EngineDecideRunner(data_client, graph=graph)
    if "work_runner" not in bindings:
        added["work_runner"] = EngineWorkRunner(control_client, graph=CONTROL_GRAPH)
    return {**bindings, **added}


def _with_ops_admin_store(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
) -> Mapping[str, Any] | None:
    """Bind ``ops_admin_store``: Arbora config plus engine Health/Backup/Restore.

    ARBORA-OPS-R024.5.  The engine is reached through the served pool under
    Arbora's verified process identity.
    """
    if bindings is None or "ops_admin_store" in bindings:
        return bindings
    from arbora.mcp_server.ops_admin_store import ArboraOpsAdminStore

    async def client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    return {**bindings, "ops_admin_store": ArboraOpsAdminStore(client, graph=graph)}


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

        from arbora.fleet.access_contracts import fleet_virtual_catalog
        from arbora.fleet.source_executor import FleetSourceBinding

        catalog = bindings.get("fleet_virtual_catalog")
        if catalog is None:
            catalog = fleet_virtual_catalog()
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
        from epistemic_graph.generated import METHOD_IDS
        from epistemic_graph.generated import query as generated_query
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


def _schema_session(session: Callable[[], Any], tenant: str, graph: str) -> Any:
    from arbora.api.contracts.session import ScopeError, resolve_session

    verified = resolve_session(session(), graph=graph)
    try:
        # EG's FeatureSchema publisher classifies this as the exact
        # admin:decision-catalog action.  A service identity carrying that
        # exact capability may use it directly; EG also explicitly treats
        # kg:admin as the aggregate grant for every admin action.  Keep the
        # aggregate fallback here rather than lowering publication to the
        # ordinary kg:write capability (which EG deliberately refuses).
        verified.require_scope("admin:decision-catalog")
    except ScopeError:
        verified.require_scope("kg:admin")
    if verified.tenant != tenant or not str(verified.policy_version).strip():
        raise PermissionError(
            "schema publication needs its verified tenant and revision"
        )
    return verified


async def _schema_publish_context(
    engine: Any,
    session: Callable[[], Any],
    graph: str,
    tenant: str,
    pin: Mapping[str, Any],
) -> dict[str, Any]:
    """Require a new durable receipt; default policy still holds this action."""
    import asyncio

    from arbora.api.contracts import PolicyDisposition, PolicyReceipt, get_action_policy
    from arbora.api.contracts.provisioning import _opaque_principal
    from arbora.api.task_plan_schema import PURPOSE_ID
    from arbora.mcp_server.decide_wiring import _mutation_context

    request = _schema_action(_schema_session(session, tenant, graph), graph, pin)
    digest = request.digest()
    decision = await asyncio.to_thread(get_action_policy(engine).decide, request)
    verified = _schema_session(session, tenant, graph)
    if _schema_action(verified, graph, pin).digest() != digest:
        raise PermissionError(
            "schema publication intent changed during policy decision"
        )
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
        raise PermissionError(
            "schema publication has no exact effect-authorizing receipt"
        )
    context = _mutation_context(verified, PURPOSE_ID)
    # AgentComponent validates the caller-shaped mutation context before EG's
    # admitted-context binder replaces its policy and process-owner fields.
    # Use the same canonical opaque principal derivation as connector-pack
    # provisioning; the native binder still authenticates and replaces the
    # authoritative carrier, tenant, and policy values.
    opaque_principal = _opaque_principal(str(verified.actor.actor_id))
    context.update(
        principal=opaque_principal,
        caller_principal=opaque_principal,
        actor_scope=opaque_principal,
    )
    return {
        **context,
        "policy_digest": f"sha256:{receipt.request_digest}",
        "policy_decision_id": receipt.receipt_id,
    }


def _task_plan_schema_publisher(
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
) -> Callable[[str, Mapping[str, Any]], Coroutine[Any, Any, str]]:
    """The process-lifecycle publisher; it installs the process authority."""
    from arbora.api.contracts.session import resolve_session, use_session
    from arbora.api.task_plan_schema import engine_publish
    from arbora.mcp_server.served_engine import ServedEngine

    async def client_for(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    engine = ServedEngine(pool)

    async def context_for(tenant: str, pin: Mapping[str, Any]) -> dict[str, Any]:
        return await _schema_publish_context(engine, session, graph, tenant, pin)

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


def _with_task_plan_schema(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
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
    publish = _task_plan_schema_publisher(pool, graph, connect_context, session)

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
    policy_mode: str = "none",
    plan_seal_key: bytes | None = None,
    bindings: Mapping[str, Any] | None = None,
    fleet_effect: Any = None,
    task_plan_config: Any = None,
    browser_boundary_factory: BrowserBoundaryFactory | None = None,
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
    bindings = _with_ops_runners(bindings, pool, graph, connect_context)
    bindings = _with_ops_admin_store(bindings, pool, graph, connect_context)
    bindings = _with_fleet_source_executor(bindings, pool, graph)
    bindings = _with_nl_federation_planner(bindings)
    bindings, process_startup = _with_task_plan_schema(
        bindings, pool, graph, connect_context, session
    )
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
        from arbora.mcp_server.effect_journal import EgEffectJournal

        changes["fleet_effect"] = fleet_effect
        # A governed child call is reserved and recorded in EG before dispatch.
        from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

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
        changes["external_effect_operations"] = frozenset(
            op_id
            for op_id in ("fleet.call", "messaging.alert", "browser.request_call")
            if registry.get(op_id) is not None
        )
    services = build_invoke_services(
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
    invocation = HostedInvocation(
        services=services,
        task_plan_config=task_plan_config,
        process_startup=process_startup,
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


class _LeaseClient:
    """The ``client`` shape ``EgPlanStore`` reads: one ``control_leases``."""

    def __init__(self, leases: PoolLeases) -> None:
        self.control_leases = leases

    def store(self, factory: Any, **kwargs: Any) -> Any:
        return factory(self, **kwargs)


def served_invocation_from_settings(
    *,
    process_session: Callable[[], Any],
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
        policy_mode=str(setting("EUNOMIA_MODE", "none") or "none"),
        plan_seal_key=bytes.fromhex(raw_key) if raw_key else None,
        bindings=bindings,
        fleet_effect=fleet_effect,
        task_plan_config=task_plan_config,
        browser_boundary_factory=browser_boundary_factory,
    )
