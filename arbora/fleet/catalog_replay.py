"""Durable replay of catalog changes (ARBORA-FLEET-R022).

Each catalog change an onboarding pass makes is one ``CatalogDeltaReceipt``
written into the engine in the same batch as the catalog triples (see
``catalog_graph.write_catalog``). The engine holds the receipts; arbora holds
none. :func:`replay_deltas` reads a tenant's receipts back in
``(generation, sequence)`` order and applies each once, so a reload rebuilds
the catalog from the engine alone.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, MutableSet
from dataclasses import dataclass
from typing import Any, Literal

from scion_sdk.ontology_namespaces import module_namespace

CAT = module_namespace("catalog")
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RECEIPT_CLASS = f"{CAT}CatalogDeltaReceipt"
_FIELDS = ("Tenant", "Generation", "Sequence", "Item", "Op", "Digest")

DeltaOp = Literal["upsert", "remove"]
Triple = tuple[str, str, "str | tuple[str]"]


class CatalogReplayRefused(Exception):
    """A replay refused with one typed code and applied nothing past it."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True, slots=True)
class CatalogDeltaReceipt:
    tenant: str
    generation: int
    sequence: int
    item_id: str
    op: DeltaOp
    item_digest: str

    @property
    def key(self) -> tuple[str, int, int, str]:
        return (self.tenant, self.generation, self.sequence, self.item_id)

    def iri(self) -> str:
        from urllib.parse import quote

        tenant, item = quote(self.tenant, safe=""), quote(self.item_id, safe="")
        return f"urn:arbora:catalog-delta:{tenant}:{self.generation}:{self.sequence}:{item}"

    def triples(self) -> list[Triple]:
        node = self.iri()
        values = (
            self.tenant,
            str(self.generation),
            str(self.sequence),
            self.item_id,
            self.op,
            self.item_digest,
        )
        out: list[Triple] = [(node, RDF_TYPE, RECEIPT_CLASS)]
        out += [
            (node, f"{CAT}delta{name}", (value,))
            for name, value in zip(_FIELDS, values, strict=True)
        ]
        return out


def receipt_query(tenant: str) -> str:
    """The SPARQL read of one tenant's receipts."""
    escaped = tenant.replace("\\", "\\\\").replace('"', '\\"')
    columns = " ".join(f"?{name.lower()}" for name in _FIELDS)
    patterns = " ".join(f"?r <{CAT}delta{name}> ?{name.lower()} ." for name in _FIELDS)
    return (
        f"SELECT {columns} WHERE {{ ?r a <{RECEIPT_CLASS}> . {patterns} "
        f'FILTER(?tenant = "{escaped}") }}'
    )


def _value(term: Any) -> str:
    return str(term["value"] if isinstance(term, Mapping) else term)


def receipts_from_rows(rows: Iterable[Iterable[Any]]) -> list[CatalogDeltaReceipt]:
    """Decode SPARQL rows into receipts, ordered by ``(generation, sequence)``."""
    found = []
    for row in rows:
        tenant, generation, sequence, item, op, digest = (_value(t) for t in row)
        found.append(
            CatalogDeltaReceipt(
                tenant,
                int(generation),
                int(sequence),
                item,
                op,
                digest,  # type: ignore[arg-type]
            )
        )
    return sorted(found, key=lambda r: (r.generation, r.sequence, r.item_id))


def next_position(receipts: Iterable[CatalogDeltaReceipt]) -> tuple[int, int]:
    """The ``(generation, sequence)`` the next receipt takes."""
    last = max(
        ((r.generation, r.sequence) for r in receipts),
        default=(1, 0),
    )
    return last[0], last[1] + 1


def plan_deltas(
    tenant: str,
    prior: Mapping[str, str],
    current: Mapping[str, str],
    position: tuple[int, int],
) -> list[CatalogDeltaReceipt]:
    """One receipt per change from ``prior`` to ``current`` item digests."""
    generation, sequence = position
    changes: list[tuple[str, DeltaOp, str]] = [
        (item, "upsert", digest)
        for item, digest in sorted(current.items())
        if prior.get(item) != digest
    ]
    changes += [
        (item, "remove", digest)
        for item, digest in sorted(prior.items())
        if item not in current
    ]
    return [
        CatalogDeltaReceipt(tenant, generation, sequence + n, item, op, digest)
        for n, (item, op, digest) in enumerate(changes)
    ]


def _check_gaps(receipts: list[CatalogDeltaReceipt]) -> None:
    last: dict[int, int] = {}
    for receipt in receipts:
        previous = last.get(receipt.generation, 0)
        if receipt.sequence != previous + 1:
            raise CatalogReplayRefused(
                "CATALOG_REPLAY_GAP",
                f"generation {receipt.generation} expected sequence {previous + 1}",
            )
        last[receipt.generation] = receipt.sequence


def replay_deltas(
    receipts: Iterable[CatalogDeltaReceipt],
    *,
    state: dict[str, str],
    applied: MutableSet[tuple[str, int, int, str]],
    stored_digest: Callable[[str], str | None] = lambda _item: None,
) -> int:
    """Apply each receipt once to ``state``; return how many were applied.

    ``state`` maps item id to the digest of the stored item. A key already in
    ``applied`` is skipped, so a second replay is a no-op. A gap in the
    sequence refuses ``CATALOG_REPLAY_GAP``; an upsert whose digest differs
    from ``stored_digest(item)`` refuses ``CATALOG_REPLAY_DIGEST_MISMATCH``.
    """
    ordered = sorted(receipts, key=lambda r: (r.generation, r.sequence, r.item_id))
    _check_gaps(ordered)
    count = 0
    for receipt in ordered:
        if receipt.key in applied:
            continue
        stored = stored_digest(receipt.item_id)
        if receipt.op == "upsert" and stored not in (None, receipt.item_digest):
            raise CatalogReplayRefused(
                "CATALOG_REPLAY_DIGEST_MISMATCH", receipt.item_id
            )
        if receipt.op == "upsert":
            state[receipt.item_id] = receipt.item_digest
        else:
            state.pop(receipt.item_id, None)
        applied.add(receipt.key)
        count += 1
    return count


async def read_receipts(
    client: Any, graph: str, tenant: str
) -> list[CatalogDeltaReceipt]:
    """The tenant's stored receipts, in replay order."""
    from medulla.generated.reasoning import send_sparql

    result = await send_sparql(client, {"query": receipt_query(tenant)}, graph)
    payload = getattr(result, "payload", result)
    rows = payload.get("rows", []) if isinstance(payload, Mapping) else []
    return receipts_from_rows(rows)


__all__ = [
    "CatalogDeltaReceipt",
    "CatalogReplayRefused",
    "next_position",
    "plan_deltas",
    "read_receipts",
    "receipt_query",
    "receipts_from_rows",
    "replay_deltas",
]
