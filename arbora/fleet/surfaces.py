"""Per-surface content counts for one fleet connector (ARBORA-FLEET-R040).

A connector serves up to five content surfaces: tools, skills, prompts,
resources and ontology (``ontology://`` and ``shapes://`` resources). A
surface the connector does not serve is reported as ``not served``, never as
a silent zero. A connector with no prompt, skill or ontology also carries one
``surface_gap`` entry for each missing kind.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

NOT_SERVED: Final = "not served"
SERVED: Final = "served"
SURFACES: Final[tuple[str, ...]] = (
    "tools",
    "skills",
    "prompts",
    "resources",
    "ontology",
)
#: The kinds whose absence is a reported gap, not just a zero.
GAP_SURFACES: Final[tuple[str, ...]] = ("prompts", "skills", "ontology")
_ONTOLOGY_SCHEMES: Final = ("ontology://", "shapes://")
_KIND_SURFACE: Final[Mapping[str, str]] = {
    "tool": "tools",
    "skill": "skills",
    "prompt": "prompts",
    "mcp_prompt": "prompts",
}


def resource_surface(uri: str) -> str:
    """The surface a resource URI belongs to."""
    return "ontology" if uri.startswith(_ONTOLOGY_SCHEMES) else "resources"


def item_surface(kind: str, name: str) -> str | None:
    """The surface of a catalog kind and name, or ``None`` for non-content."""
    if kind in {"resource", "resource_template", "mcp_resource"}:
        return resource_surface(name)
    return _KIND_SURFACE.get(kind)


def empty_counts() -> dict[str, int]:
    return dict.fromkeys(SURFACES, 0)


def count_items(pairs: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Count ``(kind, name)`` pairs per surface."""
    counts = empty_counts()
    for kind, name in pairs:
        surface = item_surface(kind, name)
        if surface is not None:
            counts[surface] += 1
    return counts


def surface_report(counts: Mapping[str, int]) -> dict[str, Any]:
    """The explicit per-surface report: counts, status and gaps."""
    surfaces = {
        name: {
            "count": counts.get(name, 0),
            "status": SERVED if counts.get(name, 0) else NOT_SERVED,
        }
        for name in SURFACES
    }
    gaps = [name for name in GAP_SURFACES if not counts.get(name, 0)]
    return {"surfaces": surfaces, "surface_gap": gaps}


def pack_surface_counts(pack: Any) -> dict[str, int]:
    """Count a captured ConnectorPack's entries per surface."""
    entries = pack.archive.entries
    return count_items(
        (str(entry.kind), str(getattr(entry, "uri", "") or entry.name))
        for entry in entries
    )


def pack_surface_report(pack: Any) -> dict[str, Any]:
    return surface_report(pack_surface_counts(pack))
