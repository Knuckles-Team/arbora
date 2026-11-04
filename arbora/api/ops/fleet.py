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
from arbora.fleet.service_child import ServiceChildOutcomeUnknown


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FleetCatalogParams(_Params):
    query: str = Field(default="", max_length=4_096)
    kinds: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    browse: bool = False
    cursor: str | None = Field(default=None, max_length=1_024)
    limit: int = Field(default=50, ge=1, le=100)
    context_budget_tokens: int | None = Field(default=None, ge=256, le=1_000_000)


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


class FleetLoadParams(_Params):
    items: tuple[str, ...] = Field(min_length=1, max_length=256)
    auto_unload: bool = False
    evict: str | None = Field(default=None, pattern="^lru$")


class FleetUnloadParams(_Params):
    items: tuple[str, ...] = ()
    servers: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    all_items: bool = False


class FleetCallParams(_Params):
    server: str = Field(min_length=1, max_length=128)
    tool: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class FleetPackStatusParams(_Params):
    connector: str = Field(min_length=1, max_length=128)


class FleetResult(BaseModel):
    value: Any


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


async def handle_fleet_operation(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Project catalog and session operations through the one bound gateway."""
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise RuntimeError("fleet gateway is not bound")
    caller = context.caller
    if op.id == "fleet.catalog.search":
        # ARBORA-FLEET-R029.1: an unscoped search with no query is a caller
        # error (HTTP 400), refused before the gateway runs.
        if not params.get("query") and not params.get("browse"):
            if not params.get("servers"):
                from arbora.api.invoke.pipeline import OperationRefused

                raise OperationRefused("INVALID_ARGUMENT", {"field": "query"})
        result = await gateway.search(caller, **params)
    elif op.id == "fleet.catalog.list":
        result = await gateway.list(caller, **params)
    elif op.id == "fleet.tools.load":
        result = await gateway.load(caller, **params)
    elif op.id == "fleet.tools.unload":
        result = await gateway.unload(caller, **params)
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
    else:
        raise ValueError("unknown fleet operation")
    return {"value": result}


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

    from arbora.fleet.plan_confirmation import confirmed_plan_call

    if _plan_confirmed(context, op):
        return confirmed_plan_call(str(params["tool"]))
    return contextlib.nullcontext()


async def _gateway_call(
    gateway: Any, context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
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
    )
    return {"value": result}


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
            )
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
    "FleetCallParams",
    "FleetCatalogParams",
    "FleetLoadParams",
    "FleetPackStatusParams",
    "FleetPromptGetParams",
    "FleetResourceReadParams",
    "FleetResult",
    "FleetStatusParams",
    "FleetSurfaceListParams",
    "FleetUnloadParams",
    "ServiceChildOutcomeUnknown",
    "handle_fleet_call",
    "handle_fleet_operation",
    "handle_pack_status",
    "operations",
    "specs",
]
