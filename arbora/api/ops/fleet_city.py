"""Fleet city layout: the engine world view's one spatial query (ARBORA-FLEET-R084).

Arbora serves the spatial layout of the fleet tenant from the Medulla world
view: districts, buildings, rooms, streets, the ``locatedIn``/``position``/
``hasInterior`` relations and the active manual overrides. It invokes the
engine's one spatial query method as the bound actor
(``MEDULLA-WORLD-VIEW-R003``) and serves its stable envelope unchanged; it
adds no placement logic and no second registry. An uncomposed world-view
port fails closed with a typed ``UNAVAILABLE``; the engine's typed
unknown-tenant and unknown-layout refusals are preserved in arbora's closed
error enum.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import (
    Params,
    build_read_op,
    build_write_op,
    read_op_models,
)
from arbora.api.registry import Confirm, OpSpec, Verb

#: The edit-layer write replays under the engine's world-view edit-layer version
#: write (ARBORA-FLEET-R087); arbora keeps no store of its own.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "fleet.city.edit_layer.put": "medulla:WorldViewEditLayer",
}

#: Engine-reported edit refusals, carried as the reason of one closed code.
_EDIT_REFUSALS = frozenset({"PIN_COLLISION", "STALE_EDIT"})

CityLayoutParams, CityLayoutResult = read_op_models("CityLayout")


@runtime_checkable
class CityLayoutPort(Protocol):
    """The one typed port Arbora calls the engine spatial query through.

    The port is composed as ``city_layout`` by the served process. It
    returns the engine's stable layout envelope, or raises a closed arbora
    refusal (``SUBJECT_ACCESS_DENIED`` for an unknown tenant, ``NOT_FOUND``
    for an unknown layout), both preserved from the engine.
    """

    async def layout(
        self, *, tenant: str, context: ExecutionContext
    ) -> Mapping[str, Any]: ...


async def handle_city_layout(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve the tenant's engine world-view layout envelope unchanged."""
    CityLayoutParams.model_validate(dict(params))
    port = context.services.get("city_layout")
    if port is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "world view engine unbound"})
    envelope = await port.layout(tenant=context.caller.tenant, context=context)
    if not isinstance(envelope, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "world view envelope malformed"}
        )
    return {"value": dict(envelope)}


class CityChunkParams(Params):
    """A cell range or an anchor node, plus the engine's opaque cursor."""

    cell_range: str | None = Field(default=None, min_length=1, max_length=256)
    anchor: str | None = Field(default=None, min_length=1, max_length=2048)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)


CityChunkResult = read_op_models("CityChunk")[1]
CityRulesParams, CityRulesResult = read_op_models("CityRules")
EditLayerGetParams, EditLayerGetResult = read_op_models("CityEditLayerGet")


class EditLayerPutParams(Params):
    edits: list[dict[str, Any]] = Field(min_length=1, max_length=1024)
    base_version: int = Field(ge=0)


EditLayerPutResult = read_op_models("CityEditLayerPut")[1]


def _port(context: Any, name: str, reason: str) -> Any:
    port = context.services.get(name)
    if port is None:
        raise OperationRefused("UNAVAILABLE", {"reason": reason})
    return port


def _envelope(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "world view envelope malformed"}
        )
    return {"value": dict(value)}


async def handle_city_chunk(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Forward tenant, range or anchor, and cursor; serve the chunk unchanged."""
    model = CityChunkParams.model_validate(dict(params))
    if (model.cell_range is None) == (model.anchor is None):
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": "give exactly one of cell_range or anchor"}
        )
    port = _port(context, "city_chunk", "world view engine unbound")
    return _envelope(
        await port.chunk(
            tenant=context.caller.tenant,
            cell_range=model.cell_range,
            anchor=model.anchor,
            cursor=model.cursor,
            context=context,
        )
    )


async def handle_edit_layer_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve the edit layer with its version; arbora stores nothing."""
    EditLayerGetParams.model_validate(dict(params))
    port = _port(context, "city_edit_layer", "world view engine unbound")
    return _envelope(await port.get(tenant=context.caller.tenant, context=context))


async def handle_edit_layer_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Write one edit-layer version; the engine validates and records the actor."""
    model = EditLayerPutParams.model_validate(dict(params))
    port = _port(context, "city_edit_layer", "world view engine unbound")
    try:
        answer = await port.put(
            tenant=context.caller.tenant,
            edits=model.edits,
            base_version=model.base_version,
            context=context,
        )
    except OperationRefused as refused:
        reason = refused.details.get("reason") if refused.details else None
        if refused.code in _EDIT_REFUSALS:
            raise OperationRefused(
                "REVISION_CONFLICT", {"reason": refused.code}
            ) from refused
        if reason in _EDIT_REFUSALS:
            raise OperationRefused("REVISION_CONFLICT", {"reason": reason}) from refused
        raise
    return _envelope(answer)


async def handle_city_rules(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve the registry, growth rules and seed read from the engine."""
    CityRulesParams.model_validate(dict(params))
    port = _port(context, "city_rules", "world view engine unbound")
    return _envelope(await port.rules(tenant=context.caller.tenant, context=context))


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.fleet_city.handle_"
    return (
        build_read_op(
            op_id="fleet.city.chunk",
            summary="Read one bounded city chunk from the engine world view",
            examples=("show a city chunk",),
            params=CityChunkParams,
            result=CityChunkResult,
            handler=h + "city_chunk",
            scope="fleet:read",
        ),
        build_read_op(
            op_id="fleet.city.edit_layer.get",
            summary="Read the city edit layer and its version",
            examples=("show the city edit layer",),
            params=EditLayerGetParams,
            result=EditLayerGetResult,
            handler=h + "edit_layer_get",
            scope="fleet:read",
        ),
        build_write_op(
            verb=Verb.WRITE,
            op_id="fleet.city.edit_layer.put",
            summary="Write one city edit-layer version",
            examples=("save my city edits",),
            params=EditLayerPutParams,
            result=EditLayerPutResult,
            handler=h + "edit_layer_put",
            scope="fleet:write",
            confirm=Confirm.PLAN,
        ),
        build_read_op(
            op_id="fleet.city.rules",
            summary="Read the city mapping registry, growth rules and seed",
            examples=("show the city growth rules",),
            params=CityRulesParams,
            result=CityRulesResult,
            handler=h + "city_rules",
            scope="fleet:read",
        ),
        build_read_op(
            op_id="fleet.city.layout",
            summary="Read the fleet tenant's city layout from the engine world view",
            examples=("show me the city layout",),
            params=CityLayoutParams,
            result=CityLayoutResult,
            handler="arbora.api.ops.fleet_city.handle_city_layout",
            scope="fleet:read",
        ),
    )
