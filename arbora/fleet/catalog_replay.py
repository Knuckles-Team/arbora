"""Durable, idempotent catalog reconciliation by ordered delta receipts (ARBORA-FLEET-R022)."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

DeltaKind = Literal["tool", "prompt", "resource", "skill"]

__all__ = ["CatalogReplayLedger", "DeltaOutOfOrderError", "DeltaReceipt"]


class DeltaOutOfOrderError(RuntimeError):
    """A delta skipped ahead of the next expected sequence for its stream."""


@dataclass(frozen=True, slots=True)
class DeltaReceipt:
    """One ordered change, keyed by tenant, generation, sequence and item id."""

    tenant: str
    generation: int
    sequence: int
    item_id: str
    kind: DeltaKind
    digest: str

    @property
    def key(self) -> tuple[str, int, int, str]:
        return (self.tenant, self.generation, self.sequence, self.item_id)


class CatalogReplayLedger:
    """Applies each receipt exactly once, in sequence order per (tenant, generation).

    A replayed receipt (same key) is a no-op; a gap in sequence refuses so a
    reconciliation never skips a change. ``replay`` re-ingests via ``apply``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._applied: set[tuple[str, int, int, str]] = set()
        self._next: dict[tuple[str, int], int] = {}

    def replay(
        self, receipts: Iterable[DeltaReceipt], apply: Callable[[DeltaReceipt], None]
    ) -> int:
        applied = 0
        with self._lock:
            for receipt in sorted(
                receipts, key=lambda r: (r.tenant, r.generation, r.sequence)
            ):
                if receipt.key in self._applied:
                    continue
                stream = (receipt.tenant, receipt.generation)
                expected = self._next.get(stream, receipt.sequence)
                if receipt.sequence > expected:
                    raise DeltaOutOfOrderError(
                        f"{stream} expected sequence {expected}, got {receipt.sequence}"
                    )
                apply(receipt)
                self._applied.add(receipt.key)
                self._next[stream] = max(expected, receipt.sequence + 1)
                applied += 1
        return applied
