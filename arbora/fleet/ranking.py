"""The ranking port: medulla chooses the order, the lexical floor answers otherwise.

ARBORA-FLEET-R083.1. A ranking of tools, skills, actions or operations is a
choice, and a choice is medulla's. Every ranking site calls one
:class:`RankingPort`. :class:`MedullaRanking` asks the engine and answers the
floor order on abstention, error or timeout, so a call never fails for want of
a ranking.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from arbora.fleet.lexical_floor import LexicalFloor

__all__ = ["MedullaRanking", "RankCandidate", "RankingPort"]

Kind = Literal["tool", "skill", "action", "operation", "catalog_item"]


@dataclass(frozen=True)
class RankCandidate:
    id: str
    name: str
    description: str = ""
    kind: Kind = "tool"


class RankingPort(Protocol):
    def rank(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> list[str]: ...


#: ``ask(question_id, query, candidates)`` returns the engine's ordered ids, or
#: ``None`` when the engine abstains.
Ask = Callable[[str, str, Sequence[RankCandidate]], "Sequence[str] | None"]


def floor_order(query: str, candidates: Sequence[RankCandidate]) -> list[str]:
    floor = LexicalFloor(query, (f"{c.name} {c.description}" for c in candidates))
    return floor.order((c.id, c.name, c.description) for c in candidates)


class MedullaRanking:
    """Ask the engine; answer the floor order when it cannot answer."""

    def __init__(self, ask: Ask | None) -> None:
        self._ask = ask
        #: ``"engine"`` or ``"floor"`` for the last call, and why the floor answered.
        self.last_source = "floor"
        self.last_reason = "decision_unavailable"

    def rank(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> list[str]:
        floor = floor_order(query, candidates)
        if self._ask is None:
            return self._floor(floor, "decision_unavailable")
        try:
            offered = self._ask(question_id, query, candidates)
        except TimeoutError:
            return self._floor(floor, "timeout")
        except Exception:  # noqa: BLE001 - a ranking never fails the call
            return self._floor(floor, "decision_unavailable")
        known = {c.id for c in candidates}
        ordered = list(dict.fromkeys(i for i in offered or () if i in known))
        if not ordered:
            return self._floor(floor, "abstained")
        self.last_source, self.last_reason = "engine", ""
        return ordered + [i for i in floor if i not in set(ordered)]

    def _floor(self, order: list[str], reason: str) -> list[str]:
        self.last_source, self.last_reason = "floor", reason
        return order
