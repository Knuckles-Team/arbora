"""Governed direct fleet call; dynamic effect comes from gateway_ops.

ARBORA-FLEET-R015/R018: the registry-facing fleet operation family. These
operations declare typed, scoped access to the dynamic fleet catalog (the
four resident tools: ``fleet.catalog.search``/``fleet.catalog.list`` project
``find_tools``'s browse and query modes, ``fleet.tools.load``/
``fleet.tools.unload`` are ``load_tools``/``unload_tools``, and
``fleet.status`` is ``multiplexer_status``) plus ``fleet.call`` — a direct,
governed child call whose effect classification is derived from the target
item's own annotations rather than from caller-supplied arguments (see
:mod:`arbora.fleet.gateway_ops`).

Fleet supervision (``fleet.health``/``fleet.topology``/``fleet.pause``/
``fleet.kill``) is a separate concern that projects the existing HTTP-only
``arbora.gateway.fleet`` surface and is intentionally not declared here
yet: that surface is Starlette-request-shaped on this repository's current
``main`` and has no caller-parametrized read functions for these operations
to call through without changing files outside this lane's fleet-catalog
scope.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    HttpShape,
    OpSpec,
    Surface,
    Verb,
)
from arbora.fleet.approval_meta import ApprovalRefused, checked_approval_ref
from arbora.fleet.service_child import ServiceChildOutcomeUnknown


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FleetCatalogParams(_Params):
    query: str = Field(default="", max_length=4_096)
    kinds: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    browse: bool = False
    #: ``fleet.catalog.list`` only: page the fleet census by server name
    #: instead of items (ARBORA-FLEET-R040.3).
    summary: bool = False
    cursor: str | None = Field(default=None, max_length=1_024)
    limit: int = Field(default=50, ge=1, le=100)
    context_budget_tokens: int | None = Field(default=None, ge=256, le=1_000_000)
    #: ``fleet.catalog.search`` only: the job whose tools the engine selects
    #: (ARBORA-FLEET-R037.3).
    job_text: str | None = Field(default=None, min_length=1, max_length=4_096)
    required_capabilities: tuple[str, ...] = ()


class FleetSurfaceListParams(_Params):
    query: str = Field(default="", max_length=4_096)
    servers: tuple[str, ...] = ()
    cursor: str | None = Field(default=None, max_length=1_024)
    limit: int = Field(default=50, ge=1, le=100)


class FleetPromptGetParams(_Params):
    server: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=1_024)
    arguments: dict[str, str] = Field(default_factory=dict)


class FleetResourceReadParams(_Params):
    server: str = Field(min_length=1, max_length=128)
    uri: str = Field(min_length=1, max_length=8_192)


class FleetStatusParams(_Params):
    servers: tuple[str, ...] = ()
    require_full_coverage: bool = False


class FleetConnectionStateParams(_Params):
    servers: tuple[str, ...] = Field(default=(), max_length=64)


class FleetLoadParams(_Params):
    items: tuple[str, ...] = Field(default=(), max_length=256)
    groups: tuple[str, ...] = ()
    auto_unload: bool = False
    evict: str | None = Field(default=None, pattern="^lru$")


class FleetUnloadParams(_Params):
    items: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    all_items: bool = False
    include_pinned: bool = False


class FleetPinParams(_Params):
    items: tuple[str, ...] = Field(min_length=1, max_length=32)


class FleetPinsListParams(_Params):
    # No ``principal`` filter: the invoke pipeline rejects caller-asserted
    # identity fields (FORBIDDEN_AUTHORITY) before dispatch, and the tenant's
    # admins page per principal with cursor instead (R059.8 deviation, filed
    # in the wave PR).
    cursor: str | None = Field(default=None, max_length=1_024)
    limit: int = Field(default=50, ge=1, le=100)


class FleetCatalogRefreshParams(_Params):
    servers: tuple[str, ...] = Field(min_length=1, max_length=32)


class FleetCallParams(_Params):
    server: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: A playbook write step cites the committed ``ConnectorWriteBack`` record (R871).
    playbook_step_id: str | None = Field(default=None, min_length=1, max_length=256)
    decision_record_digest: str | None = Field(default=None, max_length=128)


class FleetPackStatusParams(_Params):
    connector: str = Field(min_length=1, max_length=128)


class FleetAcceptanceParams(_Params):
    connectors: tuple[str, ...] = Field(default=(), max_length=128)
    expected_skip: dict[str, str] = Field(default_factory=dict)
    probes: dict[str, dict[str, Any]] = Field(default_factory=dict)


class FleetResult(BaseModel):
    value: Any


class FleetConvergenceParams(_Params):
    pass


class ReplicaGeneration(BaseModel):
    """One replica's active catalog generation and convergence state."""

    model_config = ConfigDict(extra="forbid")

    replica: str
    state: str
    reason: str | None = None
    generation: dict[str, Any] | None = None
    engine_generation: int


class ConvergenceResult(BaseModel):
    """The rows of ``fleet.catalog.convergence``; reading writes nothing."""

    model_config = ConfigDict(extra="forbid")

    replicas: list[ReplicaGeneration]


class ConnectionRow(BaseModel):
    """One running connector's connection state; no endpoint or credential is carried."""

    model_config = ConfigDict(extra="forbid")

    server: str
    state: str | None = None
    breaker: str | None = None
    restart_count: int | None = None
    mounted_tools: int | None = None


class ConnectionStateResult(BaseModel):
    """The rows of ``fleet.connections.state``, ordered by connector name."""

    model_config = ConfigDict(extra="forbid")

    connectors: list[ConnectionRow]


#: The engine method that holds connector packs. Its ``status`` variant is an
#: authenticated, tenant-bound read; every other variant is a governed write
#: served as ``eg.storage.ConnectorPack``.
_CONNECTOR_PACK = "ConnectorPack"


async def handle_pack_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read one connector's durable pack status for the verified caller's tenant.

    ARBORA-FLEET-R041.16: the read is the engine's ``ConnectorPack`` ``status``
    variant, sent as a read. The tenant is the verified caller's, never a
    request field.
    """
    from medulla import EngineResponseError

    from arbora.api.errors import EngineRefusal
    from arbora.api.invoke.bootstrap import dispatch_eg_method
    from arbora.api.registry import EgMethod

    request = {
        "connector": params["connector"],
        "tenant_id": str(context.caller.tenant),
    }
    try:
        result = await dispatch_eg_method(
            EgMethod(service=_CONNECTOR_PACK, op=_CONNECTOR_PACK),
            {"op": {"op": "status", "request": request}},
            context,
        )
    except EngineResponseError as exc:
        raise EngineRefusal(exc.code) from exc
    value = getattr(result, "payload", result)
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return {"value": value}


async def handle_fleet_acceptance(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Run the four acceptance checks per connector (ARBORA-RELEASE-R003.9).

    Read-only. The checks read ``fleet.status``, the engine ``ConnectorPack``
    status, one read-only ``fleet.call`` probe and the onboarding digest
    comparison; Arbora records nothing. ``blocked`` carries
    ``ACCEPTANCE_BLOCKED``.
    """
    from arbora.fleet.acceptance import check_fleet
    from arbora.fleet.acceptance_ports import ContextAcceptancePorts

    ports = ContextAcceptancePorts(
        context,
        pack_status=handle_pack_status,
        pack_op=op,
        probes={name: dict(probe) for name, probe in params["probes"].items()},
    )
    connectors = tuple(params["connectors"]) or await ports.configured()
    receipt = await check_fleet(ports, connectors, params["expected_skip"])
    if receipt["state"] == "blocked":
        receipt["refusal"] = "ACCEPTANCE_BLOCKED"
    return {"value": receipt}


async def _find_for_job(
    context: Any, gateway: Any, job_text: str, capabilities: tuple[str, ...]
) -> dict[str, Any]:
    """One engine ``LoadSet`` decision for a job; the tenant is the caller's."""
    from medulla.decision_client import load_set_request

    from arbora.api.invoke.bootstrap import caller_dispatch_graph
    from arbora.api.ops.engine_calls import engine_send
    from arbora.api.ops.skills_tier import _feature_schema
    from arbora.fleet.job_loads import JobLoadRequest, find_for_job

    tenant = str(context.caller.tenant)
    graph = await caller_dispatch_graph(context)

    async def decide(question: dict[str, Any]) -> Mapping[str, Any]:
        request = load_set_request(
            tenant,
            "job-load-set",
            {**question, "graph": graph},
            _feature_schema(context, tenant),
        )
        return await engine_send(context, "Decide", {"request": request})

    return await find_for_job(
        gateway,
        context.caller,
        decide,
        JobLoadRequest(job_text, tuple(capabilities)),
    )


async def handle_fleet_operation(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Project catalog and session operations through the one bound gateway."""
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise RuntimeError("fleet gateway is not bound")
    caller = context.caller
    job_text = params.get("job_text")
    capabilities = params.get("required_capabilities") or ()
    params = {
        k: v
        for k, v in params.items()
        if k not in ("job_text", "required_capabilities")
    }
    if job_text is not None or capabilities:
        if op.id != "fleet.catalog.search" or job_text is None:
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused(
                "INVALID_ARGUMENT", {"field": "job_text", "reason": "search only"}
            )
        return {"value": await _find_for_job(context, gateway, job_text, capabilities)}
    if op.id == "fleet.catalog.search":
        summary = params.get("summary")
        params = {key: value for key, value in params.items() if key != "summary"}
        if summary:
            from arbora.api.invoke.pipeline import OperationRefused

            # ARBORA-FLEET-R040.3: the census is a list, never a search.
            raise OperationRefused(
                "INVALID_ARGUMENT", {"field": "summary", "reason": "list only"}
            )
        # ARBORA-FLEET-R029.1: an unscoped search with no query is a caller
        # error (HTTP 400), refused before the gateway runs.
        if not params.get("query") and not params.get("browse"):
            if not (params.get("servers") or params.get("groups")):
                from arbora.api.invoke.pipeline import OperationRefused

                raise OperationRefused("INVALID_ARGUMENT", {"field": "query"})
        result = await gateway.search(caller, **params)
    elif op.id == "fleet.catalog.list":
        result = await gateway.list(caller, **params)
    elif op.id == "fleet.tools.load":
        if not params.get("items") and not params.get("groups"):
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused(
                "INVALID_ARGUMENT", {"reason": "a load needs items or groups"}
            )
        result = await gateway.load(caller, **params)
    elif op.id == "fleet.tools.unload":
        result = await gateway.unload(caller, **params)
    elif op.id == "fleet.tools.pin":
        result = await gateway.pin(caller, **params)
    elif op.id == "fleet.tools.unpin":
        result = await gateway.unpin(caller, **params)
    elif op.id == "fleet.catalog.refresh":
        result = await gateway.catalog_refresh(caller, **params)
    elif op.id == "fleet.pins.list":
        result = await gateway.pins_list(caller, **params)
    elif op.id == "fleet.prompts.list":
        result = await gateway.prompts_list(caller, **params)
    elif op.id == "fleet.prompts.get":
        result = await gateway.prompts_get(caller, **params)
    elif op.id == "fleet.resources.list":
        result = await gateway.resources_list(caller, **params)
    elif op.id == "fleet.resources.read":
        result = await gateway.resources_read(caller, **params)
    elif op.id == "fleet.status":
        result = await gateway.status(caller, **params)
        if params.get("require_full_coverage"):
            coverage = result.get("coverage")
            if not isinstance(coverage, Mapping) or coverage.get("ok") is not True:
                from arbora.api.invoke.pipeline import OperationRefused

                coverage = coverage if isinstance(coverage, Mapping) else {}
                details: dict[str, Any] = {
                    "missing": list(coverage.get("missing", ())),
                    "zero_tools": list(coverage.get("zero_tools", ())),
                }
                if coverage.get("error"):
                    details["error"] = coverage["error"]
                    details["detail"] = coverage.get("detail", "")
                raise OperationRefused("UNAVAILABLE", details)
    else:
        raise ValueError("unknown fleet operation")
    return {"value": result}


#: The fields of one child's health a connection-state row carries. Nothing a
#: credential or an endpoint could sit in is projected.
_CONNECTION_FIELDS = ("state", "breaker", "restart_count", "mounted_tools")


async def handle_connection_state(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Per-connector connection state, read from the one child-health producer.

    ARBORA-FLEET-R089: each row is one connector the multiplexer runs, with the
    state the runtime reports. A connector that is only registered and has no
    running child has no row; absence is not a state Arbora invents.
    """
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise RuntimeError("fleet gateway is not bound")
    snapshot = await gateway.status(context.caller, servers=tuple(params["servers"]))
    children = snapshot.get("children") if isinstance(snapshot, Mapping) else None
    if not isinstance(children, Mapping):
        raise RuntimeError("fleet status carries no children")
    rows = [
        {
            "server": name,
            **{key: child[key] for key in _CONNECTION_FIELDS if key in child},
        }
        for name, child in sorted(children.items())
        if isinstance(child, Mapping)
    ]
    return {"connectors": rows}


async def handle_catalog_convergence(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Each replica's catalog generation and state, from the one catalog reader.

    ARBORA-FLEET-R051.2.1: a read of ``FleetCatalogReader.generation_status``.
    A generation of another tenant is refused as ``TENANT_DENIED``.
    """
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.fleet.health import generation_payload

    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise RuntimeError("fleet gateway is not bound")
    status = await gateway.generation_status(context.caller)
    if status.active is not None and status.active.tenant != str(context.caller.tenant):
        raise OperationRefused("TENANT_DENIED")
    return {"replicas": [generation_payload(status)]}


async def require_step_decision(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> None:
    """A playbook write step needs a committed record of its own step and tenant.

    A missing or mismatched record refuses ``POLICY_DENIED`` with zero connector calls;
    the engine only proposes and arbora keeps execution authority (ARBORA-OPS-R871).
    """
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops._decide import committed_record

    step = params.get("playbook_step_id")
    if not step or op.effect == Effect.READ:
        return
    digest = params.get("decision_record_digest")
    record = await committed_record(context, digest) if digest else {}
    if record.get("kind") != "ConnectorWriteBack":
        raise OperationRefused("POLICY_DENIED", {"reason": "step_decision_missing"})
    if record.get("step_id") != step or str(record.get("tenant")) != str(
        context.caller.tenant
    ):
        raise OperationRefused("POLICY_DENIED", {"reason": "step_decision_mismatch"})


async def handle_fleet_call(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Require the injected gateway, which rechecks item authority at call time.

    A durable-dispatch outcome the service child itself cannot confirm
    (:class:`~arbora.fleet.service_child.ServiceChildOutcomeUnknown`)
    propagates as-is: translating it into a typed, invoke-pipeline-level
    refusal belongs to the served invocation surface once that surface
    exists on this repository, not to this operation handler.
    """
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise RuntimeError("fleet gateway is not bound")
    await require_step_decision(context, params, op)
    with _plan_confirmation(context, params, op):
        return await _gateway_call(gateway, context, params, op)


def _plan_confirmed(context: Any, op: OpSpec) -> bool:
    """A destructive call the pipeline dispatches under a consumed plan.

    The pipeline dispatches a plan-confirmed call with the key ``plan:<ref>``
    only after it validated and consumed that plan; an expired or mismatched
    ``plan_ref`` never reaches dispatch.
    """
    key = getattr(context, "idempotency_key", None)
    return (
        op.effect == Effect.DESTRUCTIVE
        and isinstance(key, str)
        and key.startswith("plan:")
    )


def _plan_confirmation(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    import contextlib

    from arbora.api.domain.fleet.plan_confirmation import confirmed_plan_call

    if _plan_confirmed(context, op):
        return confirmed_plan_call(str(params["tool"]))
    return contextlib.nullcontext()


async def _gateway_call(
    gateway: Any, context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward the call, admitted and recorded per partition when capacity is bound."""

    async def forward() -> dict[str, Any]:
        return await _forward(gateway, context, params, op)

    return await _capacity_guard(context).run(
        lambda: _capacity_partition(context, params, op), forward
    )


def _capacity_guard(context: Any) -> Any:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.fleet.throttle_service import UNGUARDED, CapacityGuard

    limiter = context.services.get("partition_limiter")
    recorder = context.services.get("outcome_recorder")
    if limiter is None or recorder is None:
        return UNGUARDED
    return CapacityGuard(
        limiter, recorder, lambda t: OperationRefused(t.code, t.details)
    )


def _capacity_partition(context: Any, params: Mapping[str, Any], op: OpSpec) -> Any:
    """Tenant, child, read or write class and policy revision of one call."""
    from arbora.api.domain.fleet.error_budget import Partition

    effect = getattr(context.fleet_decision, "effect", op.effect)
    return Partition(
        tenant=str(context.caller.tenant),
        child=str(params["server"]),
        operation_class="read" if effect == Effect.READ else "write",
        policy_revision=str(context.caller.policy_revision),
    )


async def _forward(
    gateway: Any, context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    approval_ref = await _approval_ref(context, params, op)
    result = await gateway.call(
        context.caller,
        params["server"],
        params["tool"],
        params["arguments"],
        expected_effect=op.effect,
        service_identity=context.service_identity,
        owner=context.owner,
        owner_ref=getattr(context, "owner_ref", None),
        fleet_decision=context.fleet_decision,
        registry_digest=context.registry_digest,
        approval_ref=approval_ref,
    )
    return {"value": result}


async def _approval_ref(context: Any, params: Mapping[str, Any], op: OpSpec) -> str:
    """The checked approval of a plan-confirmed call; refuses before the call.

    The plan lease already bound the arguments, so the check compares the
    approval's operation only (ARBORA-OPS-R009.5).
    """
    from arbora.api.invoke.pipeline import OperationRefused

    key = getattr(context, "idempotency_key", None)
    if not (key or "").startswith("plan:"):
        return ""
    try:
        return await checked_approval_ref(
            context.client.control_leases,
            tenant=context.caller.tenant,
            idempotency_key=key,
            op_id=op.id,
        )
    except ApprovalRefused as refusal:
        raise OperationRefused(refusal.code) from refusal


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="fleet.call",
            verb=Verb.ACT,
            summary="Call an authorized fleet child tool without loading it",
            examples=("call this fleet tool with these arguments",),
            params=FleetCallParams,
            result=FleetResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_fleet_call"),
            scopes=frozenset({"mcp:delegate"}),
            http=HttpShape(method="POST", path="/api/v1/fleet/call"),
            effect=Effect.WRITE,
            audit=AuditClass.EVENT,
        ),
        *(
            OpSpec(
                id=f"fleet.catalog.{action}",
                verb=Verb.FIND,
                summary=f"{action.capitalize()} caller-visible fleet catalog items",
                examples=("find authorized fleet tools and skills",),
                params=FleetCatalogParams,
                result=FleetResult,
                binding=Composite(
                    handler="arbora.api.ops.fleet.handle_fleet_operation"
                ),
                scopes=frozenset({"mcp:discover"}),
                http=(
                    HttpShape(method="GET", path="/api/v1/fleet/catalog")
                    if action == "search"
                    else None
                ),
                effect=Effect.READ,
            )
            for action in ("search", "list")
        ),
        *(
            OpSpec(
                id=f"fleet.tools.{action}",
                verb=Verb.MANAGE,
                summary=summary,
                examples=(example,),
                params=params_cls,
                result=FleetResult,
                binding=Composite(
                    handler="arbora.api.ops.fleet.handle_fleet_operation"
                ),
                scopes=frozenset({"mcp:delegate"}),
                effect=Effect.WRITE,
                surfaces=frozenset({Surface.MCP}),
                audit=AuditClass.EVENT,
            )
            for action, params_cls, summary, example in (
                (
                    "load",
                    FleetLoadParams,
                    "Load authorized fleet items for this MCP session",
                    "load these tools for this session",
                ),
                (
                    "unload",
                    FleetUnloadParams,
                    "Unload fleet items from this MCP session",
                    "unload my session tools",
                ),
                (
                    "pin",
                    FleetPinParams,
                    "Pin loaded fleet items so they stay loaded across sessions and restarts",
                    "pin these tools for my session",
                ),
                (
                    "unpin",
                    FleetPinParams,
                    "Unpin fleet items so they may be evicted again",
                    "unpin these tools",
                ),
            )
        ),
        OpSpec(
            id="fleet.catalog.refresh",
            verb=Verb.MANAGE,
            summary="Re-read connector tool catalogs now, without an arbora restart",
            examples=("refresh the tool catalog of craft-suite-api",),
            params=FleetCatalogRefreshParams,
            result=FleetResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_fleet_operation"),
            scopes=frozenset({"fleet:control"}),
            effect=Effect.WRITE,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="fleet.pins.list",
            verb=Verb.ASK,
            summary="List every pin in the caller's tenant with its suspended state",
            examples=("list the pinned fleet tools in this tenant",),
            params=FleetPinsListParams,
            result=FleetResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_fleet_operation"),
            scopes=frozenset({"fleet:control", "mcp:admin"}),
            effect=Effect.READ,
            audit=AuditClass.NONE,
        ),
        *(
            OpSpec(
                id=f"fleet.{family}.{action}",
                verb=verb,
                summary=summary,
                examples=(example,),
                params=params_cls,
                result=FleetResult,
                binding=Composite(
                    handler="arbora.api.ops.fleet.handle_fleet_operation"
                ),
                scopes=frozenset({scope}),
                effect=Effect.READ,
                audit=audit,
            )
            for family, action, verb, params_cls, scope, audit, summary, example in (
                (
                    "prompts",
                    "list",
                    Verb.FIND,
                    FleetSurfaceListParams,
                    "mcp:discover",
                    AuditClass.NONE,
                    "List caller-visible connector prompts (prompts/list relay)",
                    "list the prompts a connector serves",
                ),
                (
                    "prompts",
                    "get",
                    Verb.ASK,
                    FleetPromptGetParams,
                    "mcp:delegate",
                    AuditClass.EVENT,
                    "Render one connector prompt (prompts/get relay)",
                    "get this connector prompt",
                ),
                (
                    "resources",
                    "list",
                    Verb.FIND,
                    FleetSurfaceListParams,
                    "mcp:discover",
                    AuditClass.NONE,
                    "List caller-visible connector resources, skill:// included "
                    "(resources/list relay)",
                    "list the resources and ontologies a connector serves",
                ),
                (
                    "resources",
                    "read",
                    Verb.ASK,
                    FleetResourceReadParams,
                    "mcp:delegate",
                    AuditClass.EVENT,
                    "Read one connector resource, such as a skill:// or "
                    "ontology:// URI (resources/read relay)",
                    "read this connector ontology resource",
                ),
            )
        ),
        OpSpec(
            id="fleet.packs.status",
            verb=Verb.ASK,
            summary="Show one connector's durable pack status in the engine",
            examples=("show the connector pack status of gitlab-mcp",),
            params=FleetPackStatusParams,
            result=FleetResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_pack_status"),
            scopes=frozenset({"agent:pack-control"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="fleet.acceptance.check",
            verb=Verb.ASK,
            summary="Run the four fleet acceptance checks for each connector",
            examples=("run the fleet acceptance checks",),
            params=FleetAcceptanceParams,
            result=FleetResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_fleet_acceptance"),
            scopes=frozenset({"fleet:read"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="fleet.catalog.convergence",
            verb=Verb.ASK,
            summary="Show each replica's catalog generation and convergence state",
            examples=("which catalog generation does each replica serve",),
            params=FleetConvergenceParams,
            result=ConvergenceResult,
            binding=Composite(
                handler="arbora.api.ops.fleet.handle_catalog_convergence"
            ),
            scopes=frozenset({"agent:pack-control"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="fleet.connections.state",
            verb=Verb.ASK,
            summary="Show each running connector's connection state, without credentials",
            examples=("which connectors are connected",),
            params=FleetConnectionStateParams,
            result=ConnectionStateResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_connection_state"),
            scopes=frozenset({"mcp:discover"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="fleet.status",
            verb=Verb.ASK,
            summary="Show child fleet health and this session's loaded items",
            examples=("show my fleet multiplexer status",),
            params=FleetStatusParams,
            result=FleetResult,
            binding=Composite(handler="arbora.api.ops.fleet.handle_fleet_operation"),
            scopes=frozenset({"mcp:discover"}),
            effect=Effect.READ,
        ),
    )


specs = operations


__all__ = [
    "ConvergenceResult",
    "FleetAcceptanceParams",
    "FleetConvergenceParams",
    "ReplicaGeneration",
    "FleetCallParams",
    "FleetConnectionStateParams",
    "FleetCatalogParams",
    "FleetCatalogRefreshParams",
    "FleetLoadParams",
    "FleetPackStatusParams",
    "FleetPinParams",
    "FleetPinsListParams",
    "FleetPromptGetParams",
    "FleetResourceReadParams",
    "FleetResult",
    "FleetStatusParams",
    "FleetSurfaceListParams",
    "FleetUnloadParams",
    "ConnectionRow",
    "ConnectionStateResult",
    "ServiceChildOutcomeUnknown",
    "handle_connection_state",
    "handle_fleet_acceptance",
    "handle_fleet_call",
    "handle_fleet_operation",
    "handle_pack_status",
    "operations",
    "specs",
]
