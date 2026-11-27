"""The graph-backed city port behind the city operations (ARBORA-FLEET-R084 to R088).

The engine serves no spatial method: spatial facts are plain graph facts, and the
layout is computed client-side. One port serves the four typed services the city
handlers read (``city_layout``, ``city_chunk``, ``city_edit_layer`` and
``city_rules``) through the engine's existing methods, sent as the verified caller:
``GetNodesByLabel``, ``GetNeighborsBatch``, ``GetNodePropertiesBatch``, ``AddNode``
and ``CompareAndSetNodeFields``. Arbora keeps no registry and stores nothing: the
edit layer is one graph node per tenant, and its write replays under the engine's
idempotency key.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import msgpack

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import decode, engine_send, node_rows
from arbora.fleet.city_layout import (
    CatalogEntry,
    override_from_properties,
    world_envelope,
)

#: Labels of the graph nodes the city reads.
OVERRIDE_LABEL = "Building"
OVERRIDE_PREFIX = "world:override:"
ARCHETYPE_LABEL = "CityArchetypeMapping"
GROWTH_RULE_LABEL = "CityGrowthRule"
SEED_LABEL = "CitySeed"
EDIT_LAYER_LABEL = "CityEditLayer"

#: One chunk, one catalog page and one label read are bounded.
CHUNK_LIMIT = 256
LABEL_LIMIT = 1000
CATALOG_PAGE = 100

#: The service names the city handlers read from ``context.services``.
CITY_SERVICES: tuple[str, ...] = (
    "city_layout",
    "city_chunk",
    "city_edit_layer",
    "city_rules",
)


def _pack(value: Mapping[str, Any]) -> bytes:
    return msgpack.packb(dict(value), use_bin_type=True)


async def _by_label(
    context: Any, label: str, *, limit: int, after: str | None = None
) -> list[tuple[str, dict[str, Any]]]:
    request: dict[str, Any] = {"label": label, "limit": limit}
    if after is not None:
        request["after"] = after
    return node_rows(await engine_send(context, "GetNodesByLabel", request))


def edit_layer_id(tenant: str) -> str:
    return f"city:edit-layer:{tenant}"


def _in_range(properties: Mapping[str, Any], box: tuple[int, int, int, int]) -> bool:
    x, z = properties.get("position_x"), properties.get("position_z")
    if not (isinstance(x, (int, float)) and isinstance(z, (int, float))):
        return False
    x0, z0, x1, z1 = box
    return x0 <= x <= x1 and z0 <= z <= z1


def _cell_box(cell_range: str) -> tuple[int, int, int, int]:
    try:
        x0, z0, x1, z1 = (int(part) for part in cell_range.split(":"))
    except ValueError as exc:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": "cell_range is x0:z0:x1:z1"}
        ) from exc
    if x0 > x1 or z0 > z1:
        raise OperationRefused("INVALID_ARGUMENT", {"reason": "cell_range is inverted"})
    return x0, z0, x1, z1


def _provenance(context: Any, node_id: str) -> dict[str, str]:
    return {"tenant": str(context.caller.tenant), "node": node_id, "source": "graph"}


def _collisions(edits: list[dict[str, Any]]) -> bool:
    """Two edits that claim one cell, or one pin placed twice."""
    cells: set[Any] = set()
    pins: set[Any] = set()
    for edit in edits:
        cell, pin = edit.get("cell"), edit.get("pin")
        if cell in cells or (pin is not None and pin in pins):
            return True
        cells.add(cell)
        pins.add(pin)
    return False


async def _server_names(gateway: Any, context: Any) -> list[str]:
    """Every fleet connector name, paged from the census."""
    names: list[str] = []
    cursor: str | None = None
    while True:
        page = await gateway.list(
            context.caller, summary=True, cursor=cursor, limit=CATALOG_PAGE
        )
        names.extend(str(row["server"]) for row in page.get("servers", ()))
        cursor = page.get("next_cursor")
        if cursor is None:
            return names


async def _catalog_entries(context: Any) -> list[CatalogEntry]:
    """Every fleet connector with its group: the census, then one batch group read."""
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "fleet catalog unbound"})
    names = await _server_names(gateway, context)
    groups = await gateway.server_groups(context.caller, names)
    return [CatalogEntry(name, str(groups.get(name) or "")) for name in names]


async def _layout(context: Any) -> dict[str, Any]:
    entries = await _catalog_entries(context)
    stored = await _by_label(context, OVERRIDE_LABEL, limit=LABEL_LIMIT)
    found = (
        override_from_properties(props)
        for node_id, props in stored
        if node_id.startswith(OVERRIDE_PREFIX)
    )
    return world_envelope(entries, [o for o in found if o is not None])


async def _range_chunk(
    context: Any, cell_range: str, cursor: str | None
) -> dict[str, Any]:
    box = _cell_box(cell_range)
    page = await _by_label(context, OVERRIDE_LABEL, limit=CHUNK_LIMIT, after=cursor)
    nodes = [
        _element(context, nid, props) for nid, props in page if _in_range(props, box)
    ]
    more = len(page) >= CHUNK_LIMIT
    return {
        "cell_range": cell_range,
        "nodes": nodes,
        "edges": [],
        "next_cursor": page[-1][0] if more else None,
    }


def _element(context: Any, node_id: str, props: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": node_id,
        "properties": dict(props),
        "provenance": _provenance(context, node_id),
    }


async def _neighbour_ids(context: Any, anchor: str, cursor: str | None) -> list[str]:
    raw = decode(
        await engine_send(context, "GetNeighborsBatch", {"node_ids": [anchor]})
    )
    found = sorted(
        str(i) for node, ids in raw or () if str(node) == anchor for i in ids
    )
    return [n for n in found if cursor is None or n > cursor]


async def _anchor_chunk(
    context: Any, anchor: str, cursor: str | None
) -> dict[str, Any]:
    neighbours = await _neighbour_ids(context, anchor, cursor)
    page = neighbours[:CHUNK_LIMIT]
    ids = [anchor, *page]
    request = {"node_ids": ids}
    props = dict(
        node_rows(await engine_send(context, "GetNodePropertiesBatch", request))
    )
    if anchor not in props:
        raise OperationRefused("NOT_FOUND", {"reason": "unknown anchor"})
    more = len(neighbours) > CHUNK_LIMIT
    return {
        "anchor": anchor,
        "nodes": [_element(context, nid, props.get(nid, {})) for nid in ids],
        "edges": [{"from": anchor, "to": nid} for nid in page],
        "next_cursor": page[-1] if more else None,
    }


async def _chunk(
    context: Any, anchor: str | None, cell_range: str | None, cursor: str | None
) -> dict[str, Any]:
    if anchor is not None:
        return await _anchor_chunk(context, anchor, cursor)
    return await _range_chunk(context, str(cell_range), cursor)


async def _layer(context: Any, tenant: str) -> dict[str, Any] | None:
    request = {"node_ids": [edit_layer_id(tenant)]}
    rows = node_rows(await engine_send(context, "GetNodePropertiesBatch", request))
    return rows[0][1] if rows else None


async def _read_layer(context: Any, tenant: str) -> dict[str, Any]:
    layer = await _layer(context, tenant) or {}
    return {
        "version": int(layer.get("version", 0)),
        "edits": list(layer.get("edits") or ()),
        "actor": layer.get("actor"),
    }


async def _write_first(context: Any, node: str, body: dict[str, Any]) -> None:
    props = _pack({"labels": [EDIT_LAYER_LABEL], **body})
    request = {"node_id": node, "properties_msgpack": props}
    await engine_send(context, "AddNode", request, idempotent=True)


async def _write_next(
    context: Any, node: str, version: int, body: dict[str, Any]
) -> None:
    request = {
        "node_id": node,
        "conditions_msgpack": _pack({"version": version}),
        "updates_msgpack": _pack(body),
    }
    swapped = decode(
        await engine_send(context, "CompareAndSetNodeFields", request, idempotent=True)
    )
    if swapped is False:
        raise OperationRefused("REVISION_CONFLICT", {"reason": "STALE_EDIT"})


async def _put_layer(
    context: Any, tenant: str, edits: list[dict[str, Any]], base_version: int
) -> dict[str, Any]:
    if _collisions(edits):
        raise OperationRefused("REVISION_CONFLICT", {"reason": "PIN_COLLISION"})
    current = await _layer(context, tenant)
    version = int(current.get("version", 0)) if current else 0
    if version != base_version:
        raise OperationRefused("REVISION_CONFLICT", {"reason": "STALE_EDIT"})
    actor = str(context.caller.principal)
    body = {"version": version + 1, "edits": edits, "actor": actor}
    node = edit_layer_id(tenant)
    if current is None:
        await _write_first(context, node, body)
    else:
        await _write_next(context, node, version, body)
    return {"version": body["version"], "actor": actor}


async def _rules(context: Any) -> dict[str, Any]:
    registry = await _by_label(context, ARCHETYPE_LABEL, limit=LABEL_LIMIT)
    growth = await _by_label(context, GROWTH_RULE_LABEL, limit=LABEL_LIMIT)
    seed = await _by_label(context, SEED_LABEL, limit=1)
    return {
        "registry": [{"id": nid, **props} for nid, props in registry],
        "growth_rules": [{"id": nid, **props} for nid, props in growth],
        "seed": {"id": seed[0][0], **seed[0][1]} if seed else None,
    }


class GraphCityPort:
    """Serve each city read and write from the tenant graph as the bound caller."""

    async def layout(self, *, tenant: str, context: Any) -> Any:
        return await _layout(context)

    async def chunk(
        self,
        *,
        tenant: str,
        cell_range: str | None,
        anchor: str | None,
        cursor: str | None,
        context: Any,
    ) -> Any:
        return await _chunk(context, anchor, cell_range, cursor)

    async def get(self, *, tenant: str, context: Any) -> Any:
        return await _read_layer(context, tenant)

    async def put(
        self,
        *,
        tenant: str,
        edits: list[dict[str, Any]],
        base_version: int,
        context: Any,
    ) -> Any:
        return await _put_layer(context, tenant, edits, base_version)

    async def rules(self, *, tenant: str, context: Any) -> Any:
        return await _rules(context)
