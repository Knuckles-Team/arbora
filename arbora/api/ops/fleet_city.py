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

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import build_read_op, read_op_models
from arbora.api.registry import OpSpec

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


def operations() -> tuple[OpSpec, ...]:
    return (
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
