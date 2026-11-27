"""The derived city layout: pure functions, no I/O (ARBORA-FLEET-R084).

Placement is derived from the connector catalog and never stored. Only manual
overrides are stored, as graph nodes the port reads. The constants and the order
rules follow the engine world-view ontology's derived-layout contract, so a
client and the engine agree on the same coordinates.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

PLOT_COLUMNS = 8
PLOT_PITCH = 20
DISTRICT_PITCH = 200
UNGROUPED = "ungrouped"
ENVELOPE_VERSION = 1


def _byte_order(value: str) -> bytes:
    return value.encode("utf-8")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    server_id: str
    group: str


@dataclass(frozen=True, slots=True)
class Placement:
    server_id: str
    district: str
    plot: int
    x: int
    z: int


@dataclass(frozen=True, slots=True)
class Override:
    server_id: str
    x: int
    z: int
    reason: str


def derive_placements(catalog: Iterable[CatalogEntry]) -> tuple[Placement, ...]:
    """Place each connector by group and server id; input order never matters."""
    seen: dict[str, str] = {}
    for entry in catalog:
        if entry.server_id in seen:
            raise ValueError(f"WORLD_VIEW_DUPLICATE_SERVER: {entry.server_id}")
        seen[entry.server_id] = entry.group or UNGROUPED
    districts = sorted(set(seen.values()), key=_byte_order)
    placements: list[Placement] = []
    for index, district in enumerate(districts):
        members = sorted(
            (sid for sid, group in seen.items() if group == district), key=_byte_order
        )
        for plot, server_id in enumerate(members):
            placements.append(
                Placement(
                    server_id=server_id,
                    district=district,
                    plot=plot,
                    x=index * DISTRICT_PITCH + (plot % PLOT_COLUMNS) * PLOT_PITCH,
                    z=(plot // PLOT_COLUMNS) * PLOT_PITCH,
                )
            )
    return tuple(placements)


def _building(placement: Placement, override: Override | None) -> dict[str, Any]:
    moved = override is not None
    return {
        "server_id": placement.server_id,
        "district": placement.district,
        "plot": placement.plot,
        "x": override.x if override else placement.x,
        "z": override.z if override else placement.z,
        "overridden": moved,
        "reason": override.reason if override else None,
    }


def world_envelope(
    catalog: Iterable[CatalogEntry], overrides: Sequence[Override]
) -> dict[str, Any]:
    """The layout envelope: districts, buildings and the stale overrides."""
    derived = derive_placements(catalog)
    by_server = {o.server_id: o for o in overrides}
    known = {p.server_id for p in derived}
    districts = sorted({p.district for p in derived}, key=_byte_order)
    return {
        "version": ENVELOPE_VERSION,
        "districts": [{"name": name, "index": i} for i, name in enumerate(districts)],
        "buildings": [_building(p, by_server.get(p.server_id)) for p in derived],
        "stale_overrides": sorted(s for s in by_server if s not in known),
    }


def override_from_properties(properties: Mapping[str, Any]) -> Override | None:
    """One stored override node's properties as an override, or ``None`` if malformed."""
    server_id = properties.get("server_id")
    reason = properties.get("override_reason")
    x, z = properties.get("position_x"), properties.get("position_z")
    if not (isinstance(server_id, str) and server_id and isinstance(reason, str)):
        return None
    if not (isinstance(x, int) and isinstance(z, int)) or isinstance(x, bool):
        return None
    return Override(server_id=server_id, x=x, z=z, reason=reason)


__all__ = [
    "CatalogEntry",
    "Override",
    "Placement",
    "derive_placements",
    "override_from_properties",
    "world_envelope",
]
