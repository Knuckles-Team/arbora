"""The engine world-view port behind the city operations (ARBORA-FLEET-R086 to R088).

One port serves the four typed services the city operations read: ``city_layout``,
``city_chunk``, ``city_edit_layer`` and ``city_rules``. Each call is one engine
method sent as the verified caller through :func:`arbora.pin_lag.medulla.send_main`,
which refuses ``UNAVAILABLE`` naming the method while the installed engine does not
serve it. The answer travels unchanged: arbora adds no placement logic, keeps no
registry and stores nothing.

PIN-LAG: the methods below are the shapes arbora sends; the engine specs on
``main`` declare none of them yet (``MEDULLA-WORLD-VIEW-R003`` serves the layout
as a client-side read). Delete a guard line with the release that serves its
method.
"""

from __future__ import annotations

from typing import Any

from arbora.pin_lag.medulla import send_main

#: The engine method each port call sends.
LAYOUT_METHOD = "WorldViewLayout"
CHUNK_METHOD = "WorldViewChunk"
EDIT_LAYER_GET_METHOD = "WorldViewEditLayerGet"
EDIT_LAYER_PUT_METHOD = "WorldViewEditLayerPut"
RULES_METHOD = "WorldViewRules"

#: The service names the city handlers read from ``context.services``.
CITY_SERVICES: tuple[str, ...] = (
    "city_layout",
    "city_chunk",
    "city_edit_layer",
    "city_rules",
)


class EngineWorldViewPort:
    """Send each city read or write to the engine world view as the bound caller."""

    async def layout(self, *, tenant: str, context: Any) -> Any:
        return await send_main(context, LAYOUT_METHOD, {"tenant_id": tenant})

    async def chunk(
        self,
        *,
        tenant: str,
        cell_range: str | None,
        anchor: str | None,
        cursor: str | None,
        context: Any,
    ) -> Any:
        params = {
            "tenant_id": tenant,
            "cell_range": cell_range,
            "anchor": anchor,
            "cursor": cursor,
        }
        return await send_main(context, CHUNK_METHOD, params)

    async def get(self, *, tenant: str, context: Any) -> Any:
        return await send_main(context, EDIT_LAYER_GET_METHOD, {"tenant_id": tenant})

    async def put(
        self,
        *,
        tenant: str,
        edits: list[dict[str, Any]],
        base_version: int,
        context: Any,
    ) -> Any:
        params = {"tenant_id": tenant, "edits": edits, "base_version": base_version}
        return await send_main(context, EDIT_LAYER_PUT_METHOD, params, idempotent=True)

    async def rules(self, *, tenant: str, context: Any) -> Any:
        return await send_main(context, RULES_METHOD, {"tenant_id": tenant})
