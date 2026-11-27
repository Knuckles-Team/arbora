"""The ranking port: medulla chooses the order, the lexical floor answers otherwise.

ARBORA-FLEET-R083.1. A ranking of tools, skills, actions or operations is a
choice, and a choice is medulla's. Every ranking site calls one
:class:`RankingPort`. :class:`MedullaRanking` asks the engine and answers the
floor order on abstention, error or timeout, so a call never fails for want of
a ranking (ARBORA-FLEET-R083.5).

The port is bounded (ARBORA-FLEET-R083.3): a candidate set above
``max_candidates`` is first narrowed by the lexical prefilter, so the engine
call stays bounded on a fleet of tens of thousands of rows.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from arbora.fleet.lexical_floor import LexicalFloor

__all__ = [
    "FLOOR_ONLY",
    "MAX_RANK_CANDIDATES",
    "HostRanking",
    "MedullaRanking",
    "RankCandidate",
    "RankingStatus",
    "Ranked",
    "RankingPort",
    "ThreadBridge",
    "engine_ask",
    "ranking_probe",
    "schemas_published",
    "run_with_port",
]

Kind = Literal["tool", "skill", "action", "operation", "catalog_item"]

#: The knob ``fleet.rank.max_candidates``: its starting value, and the engine's
#: ``MAX_RANK_CATALOG_CANDIDATES`` (``CANDIDATE_SET_TOO_LARGE`` above it).
MAX_RANK_CANDIDATES = 2000

#: The four question ids medulla publishes for capability ranking
#: (``MEDULLA-DECISION-ENGINE-R829.1``). A tool row asks ``catalog.operation_rank``
#: with ``rank.kind`` ``tool``: the engine holds no second ``catalog.tool_rank`` route.
QUESTION_IDS = frozenset(
    {
        "catalog.skill_rank",
        "catalog.operation_rank",
        "catalog.catalog_item_rank",
        "catalog.intent_action_rank",
    }
)


@dataclass(frozen=True)
class RankCandidate:
    id: str
    name: str
    description: str = ""
    kind: Kind = "tool"


@dataclass(frozen=True)
class RankingStatus:
    """Which order the port serves (``medulla`` or ``floor``) and the reason."""

    source: Literal["medulla", "floor"]
    reason: str = ""

    def summary(self) -> str:
        return (
            f"ranking: {self.source} ({self.reason})"
            if self.reason
            else "ranking: " + self.source
        )


@dataclass(frozen=True)
class Ranked:
    """An order, who produced it, and why the floor answered when it did."""

    order: list[str]
    source: Literal["medulla", "floor"]
    reason: str = ""


class RankingPort(Protocol):
    def rank(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> list[str]: ...


#: ``ask(question_id, query, candidates)`` returns the engine's ordered ids, or
#: ``None`` when the engine abstains. It may be a coroutine function.
Ask = Callable[
    [str, str, Sequence[RankCandidate]],
    "Sequence[str] | None | Awaitable[Sequence[str] | None]",
]


def floor_order(query: str, candidates: Sequence[RankCandidate]) -> list[str]:
    floor = LexicalFloor(query, (f"{c.name} {c.description}" for c in candidates))
    return floor.order((c.id, c.name, c.description) for c in candidates)


def _record(source: str) -> None:
    from arbora.observability.gateway_metrics import RANKING_TOTAL

    RANKING_TOTAL.labels(source=source).inc()


class MedullaRanking:
    """Ask the engine; answer the floor order when it cannot answer."""

    def __init__(
        self, ask: Ask | None = None, *, max_candidates: int = MAX_RANK_CANDIDATES
    ) -> None:
        self._ask = ask
        self._max = max_candidates
        self._published: Callable[[], bool] | None = None

    @property
    def configured(self) -> bool:
        """Whether an engine ask is bound (an unbound port only ever answers the floor)."""
        return self._ask is not None

    def status(self) -> RankingStatus:
        """The order the port serves now, and why it is the floor when it is.

        The reason is never silent: ``/health/ready`` and ``ops.doctor`` print it.
        """
        if self._ask is None:
            return RankingStatus("floor", "engine unavailable")
        published = self._published
        if published is not None and not published():
            return RankingStatus("floor", "engine schema unpublished")
        return RankingStatus("medulla", "")

    def _narrow(
        self, query: str, candidates: Sequence[RankCandidate], floor: list[str]
    ) -> Sequence[RankCandidate]:
        if len(candidates) <= self._max:
            return candidates
        keep = set(floor[: self._max])
        return [c for c in candidates if c.id in keep]

    @staticmethod
    def _settle(
        offered: Sequence[str] | None,
        candidates: Sequence[RankCandidate],
        floor: list[str],
    ) -> Ranked:
        known = {c.id for c in candidates}
        ordered = list(dict.fromkeys(i for i in offered or () if i in known))
        if not ordered:
            return Ranked(floor, "floor", "abstained")
        placed = set(ordered)
        return Ranked(ordered + [i for i in floor if i not in placed], "medulla")

    def ordered(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> Ranked:
        """The order of one synchronous ask; a coroutine ask answers the floor here."""
        floor = floor_order(query, candidates)
        if self._ask is None:
            return self._counted(Ranked(floor, "floor", "decision_unavailable"))
        narrowed = self._narrow(query, candidates, floor)
        try:
            offered = self._ask(question_id, query, narrowed)
            if inspect.isawaitable(offered):
                if inspect.iscoroutine(offered):
                    offered.close()
                return self._counted(Ranked(floor, "floor", "decision_unavailable"))
        except TimeoutError:
            return self._counted(Ranked(floor, "floor", "timeout"))
        except Exception:  # noqa: BLE001 - a ranking never fails the call
            return self._counted(Ranked(floor, "floor", "decision_unavailable"))
        settled = self._settle(offered, narrowed, floor_of(floor, narrowed))
        return self._counted(self._tail(settled, floor))

    async def aordered(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> Ranked:
        """The order of one ask, awaiting a coroutine ask."""
        floor = floor_order(query, candidates)
        if self._ask is None:
            return self._counted(Ranked(floor, "floor", "decision_unavailable"))
        narrowed = self._narrow(query, candidates, floor)
        try:
            offered = self._ask(question_id, query, narrowed)
            if inspect.isawaitable(offered):
                offered = await offered
        except TimeoutError:
            return self._counted(Ranked(floor, "floor", "timeout"))
        except Exception:  # noqa: BLE001 - a ranking never fails the call
            return self._counted(Ranked(floor, "floor", "decision_unavailable"))
        settled = self._settle(offered, narrowed, floor_of(floor, narrowed))
        return self._counted(self._tail(settled, floor))

    @staticmethod
    def _tail(settled: Ranked, floor: list[str]) -> Ranked:
        placed = set(settled.order)
        return Ranked(
            settled.order + [i for i in floor if i not in placed],
            settled.source,
            settled.reason,
        )

    @staticmethod
    def _counted(result: Ranked) -> Ranked:
        _record(result.source)
        return result

    def rank(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> list[str]:
        return self.ordered(question_id, query, candidates).order

    async def arank(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> list[str]:
        return (await self.aordered(question_id, query, candidates)).order


#: The unbound port: every ranking site's default, answering the lexical floor.
FLOOR_ONLY = MedullaRanking()


def floor_of(floor: list[str], candidates: Sequence[RankCandidate]) -> list[str]:
    """The floor order restricted to ``candidates`` (the narrowed set)."""
    keep = {c.id for c in candidates}
    return [i for i in floor if i in keep]


class HostRanking(MedullaRanking):
    """The host's one ranking port, bound to the engine once the invocation exists.

    The host builds its fleet state before the served invocation (which owns the
    engine sender), so the port starts unbound and answers the floor; ``bind``
    hands it the engine ``Decide`` sender, the tenant feature-schema pin and the
    service principal (ARBORA-FLEET-R083.5).
    """

    def bind(self, ask: Ask, *, published: Callable[[], bool] | None = None) -> None:
        """Bind the engine ask; ``published`` says whether the engine's feature
        schemas for the ranking questions are pinned (an unpublished schema
        keeps the floor, and :meth:`status` says so)."""
        if self._ask is not None:
            raise RuntimeError("the ranking port is already bound")
        self._ask = ask
        self._published = published


def schemas_published(
    feature_schema: Callable[[str], Mapping[str, Any]],
    tenant: Callable[[], str],
    rank_schema: Callable[[str, str], Mapping[str, Any] | None] | None = None,
) -> Callable[[], bool]:
    """Whether the tenant's pin is a ranking question's ``decide.schema.<id>``.

    Until the engine publishes the ``decide.schema.catalog.*`` components the pin
    is another question's and every ranking asks answer the floor.
    """

    def published() -> bool:
        return _rank_pinned(rank_schema, tenant()) or _tenant_pin_ranks(
            feature_schema, tenant()
        )

    return published


def _question_schema(
    tenant: str,
    question_id: str,
    feature_schema: Callable[[str], Mapping[str, Any]],
    rank_schema: Callable[[str, str], Mapping[str, Any] | None] | None,
) -> dict[str, Any]:
    """The published per-question pin; else the tenant pin (another question's until
    the engine's rank schemas are published, so the ask answers the floor)."""
    published = rank_schema(tenant, question_id) if rank_schema else None
    return dict(published or feature_schema(tenant))


def _rank_pinned(
    rank_schema: Callable[[str, str], Mapping[str, Any] | None] | None, tenant: str
) -> bool:
    """Whether any ranking question has a published per-question pin."""
    if rank_schema is None:
        return False
    return any(rank_schema(tenant, q) for q in sorted(QUESTION_IDS))


def _tenant_pin_ranks(
    feature_schema: Callable[[str], Mapping[str, Any]], tenant: str
) -> bool:
    try:
        return _pin_names_a_question(dict(feature_schema(tenant)))
    except Exception:  # noqa: BLE001 - an unreadable pin is an unpublished one
        return False


def _pin_names_a_question(schema: Mapping[str, Any]) -> bool:
    return any(
        schema.get("component_id") == f"decide.schema.{q}" for q in sorted(QUESTION_IDS)
    )


def ranking_probe(port: MedullaRanking) -> Callable[[Any], dict[str, Any]]:
    """The ``ranking`` health check: the floor is visible, never silent.

    ``degraded`` is informational (it never fails readiness): a floor order is a
    working answer, and the check names why it is not the engine's.
    """

    def probe(_session: Any) -> dict[str, Any]:
        status = port.status()
        detail = {"ranking": status.source, "summary": status.summary()}
        if status.source == "medulla":
            return {"name": "ranking", "status": "ok", "detail": detail}
        return {
            "name": "ranking",
            "status": "degraded",
            "reason": status.reason,
            "detail": detail,
        }

    return probe


def _rank_catalog(options: Sequence[Any]) -> dict[str, Any]:
    """``CandidateSource::RankCatalog`` (``medulla.decision_client.rank_catalog``).

    PIN-LAG: the released medulla client lacks the builder, so the one wire shape is
    built here from the released ``DeclaredOption``; a pinned engine that does not
    serve the variant refuses, and the port answers the floor. Delete with the re-pin.
    """
    ordered = sorted(options, key=lambda o: o.option_id)
    if len({o.option_id for o in ordered}) != len(ordered):
        raise ValueError("rank catalog option ids must be unique")
    return {"source": "rank_catalog", "options": [o.wire() for o in ordered]}


def engine_ask(
    send: Callable[[str, dict[str, Any], str], Awaitable[Any]],
    *,
    tenant: Callable[[], str],
    feature_schema: Callable[[str], Mapping[str, Any]],
    principal: Callable[[], str],
    rank_schema: Callable[[str, str], Mapping[str, Any] | None] | None = None,
) -> Ask:
    """``Ask`` over the engine ``Decide`` method with a ``RankCatalog`` candidate set.

    The request carries texts only, never a score (``MEDULLA-DECISION-ENGINE-R829.1``).
    A question id the engine has not published, or a feature-schema pin that is not
    the question's own, raises: the port reads a raise as unavailable and answers the
    floor.
    """

    async def ask(
        question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> Sequence[str] | None:
        from medulla.decision_client import DeclaredOption, decide_request, param

        if question_id not in QUESTION_IDS:
            raise LookupError(f"{question_id} is not a published ranking question")
        who = tenant()
        schema = _question_schema(who, question_id, feature_schema, rank_schema)
        if schema.get("component_id") != f"decide.schema.{question_id}":
            raise LookupError("no feature schema is pinned for this ranking question")
        options = [
            DeclaredOption(
                c.id,
                texts={
                    "rank.description": c.description,
                    "rank.kind": c.kind,
                    "rank.name": c.name,
                },
            )
            for c in candidates
        ]
        request = decide_request(
            who,
            (question_id, "rank", "ordinary"),
            _rank_catalog(options),
            schema,
            params=[param("query", "text", query)],
        )
        from arbora.deployment.placement import (
            check_receipt,
            send_decide,
            single_record,
        )

        record = single_record(await send_decide(send, request, who))
        check_receipt(record, request, principal())
        outcome = record.get("outcome") or {}
        if outcome.get("outcome") != "advisory":
            return None
        return [str(s["option_id"]) for s in outcome.get("scores") or ()]

    return ask


class ThreadBridge:
    """A synchronous port over an asynchronous one, for work run in a worker thread.

    The synchronous ranking sites run under :func:`run_with_port`, so their ask may
    be the engine's coroutine: the thread waits on the host loop while the loop
    stays free to serve it.
    """

    def __init__(self, port: RankingPort, loop: asyncio.AbstractEventLoop):
        self._port = port
        self._loop = loop

    @property
    def configured(self) -> bool:
        return bool(getattr(self._port, "configured", False))

    def ordered(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> Ranked:
        aordered = getattr(self._port, "aordered", None)
        if aordered is None:
            return Ranked(self._port.rank(question_id, query, candidates), "medulla")
        return asyncio.run_coroutine_threadsafe(
            aordered(question_id, query, candidates), self._loop
        ).result()

    def rank(
        self, question_id: str, query: str, candidates: Sequence[RankCandidate]
    ) -> list[str]:
        return self.ordered(question_id, query, candidates).order


async def run_with_port(
    port: RankingPort, fn: Callable[..., Any], /, *args: Any, **kwargs: Any
) -> Any:
    """Run a synchronous ranking site off the loop with ``ranking=`` bridged to ``port``."""
    bridge = ThreadBridge(port, asyncio.get_running_loop())
    return await asyncio.to_thread(fn, *args, ranking=bridge, **kwargs)
