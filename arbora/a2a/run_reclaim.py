"""Reclaim of A2A runs whose lease expired (ARBORA-A2A-R001.2.2).

A host that dies mid-run leaves a leased WorkItem whose lease then lapses. This
consumer finds such an item, claims it by name and resumes the run from
``EngineHostCheckpoint`` under the run's ``agent_run`` delegation, the same way
the tool-task consumer proves ``authority_ref`` before it acts: the claim's
``delegation_id`` must name a delegation whose principal is the run's owner, else
the run ends ``failed`` and no step runs. It holds no state
that correctness depends on: the item, its lease fence, the delegation and the
event log live in the engine.

The engine refuses the named claim with ``DELEGATION_INVALID`` when the delegation
is revoked, expired or no longer held, and it does not fail the item. The consumer
finishes it: a final ``failed`` event goes through the event log first, then
``CancelWorkItem`` makes the item terminal, so a stream delivers ``failed`` only
once the item is terminal (``ARBORA-A2A-R001.4.1``). All time comes from the
injected clock and waiting from the injected ``sleep``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from arbora import work_items
from arbora.security.session import use_session

from .event_log import FAILURE_KEY
from .run_delegation import (
    DELEGATION_INVALID,
    DELEGATION_KEY,
    RunDelegationRefused,
    delegation_refused,
)

__all__ = [
    "A2ARunReclaimConsumer",
    "A2ARunReclaimService",
    "ExpiredLeaseCandidates",
    "ReclaimCandidate",
    "ReclaimOutcome",
    "build_run_reclaim_service",
]

_KIND = "orchestrator_task"
_SCHEMA = "arbora-a2a-unary-v1"
_PREFIX = "workitem:orchestrator:"
_HELD = frozenset({"leased", "running"})
_PAGE = 100
DEFAULT_POLL_INTERVAL_S = 2.0
_POLL_RANGE = (0.1, 60.0)


@dataclass(frozen=True, slots=True)
class ReclaimCandidate:
    """One expired-lease item and the verified session of its tenant and owner."""

    session: Any
    item: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ReclaimOutcome:
    item_id: str
    #: ``resumed``, ``failed`` (the delegation was refused) or ``skipped``.
    outcome: str


class ExpiredLeaseCandidates:
    """The A2A items of each tenant whose lease expired on the injected clock."""

    def __init__(
        self,
        work_engine_provider: Callable[[], Any],
        sessions: Callable[[], Iterable[Any]],
        *,
        clock: Callable[[], float],
    ) -> None:
        self._engine = work_engine_provider
        self._sessions = sessions
        self._clock = clock

    def __call__(self) -> list[ReclaimCandidate]:
        now_ms = int(self._clock() * 1000)
        found: list[ReclaimCandidate] = []
        for session in self._sessions():
            with use_session(session):
                cursor: str | None = None
                while True:
                    page = work_items.list_work_items(
                        self._engine(), kind=_KIND, cursor=cursor, limit=_PAGE
                    )
                    for item in page.get("items", ()):
                        if self.expired(item, now_ms):
                            found.append(ReclaimCandidate(session, item))
                    cursor = page.get("next_cursor")
                    if not cursor:
                        break
        return found

    @staticmethod
    def expired(item: Mapping[str, Any], now_ms: int) -> bool:
        """A held, delegation-bearing A2A item whose lease horizon has passed."""
        metadata = item.get("metadata")
        lease = item.get("lease_expires_at_ms")
        return (
            isinstance(metadata, Mapping)
            and metadata.get("a2a_schema") == _SCHEMA
            and bool(metadata.get(DELEGATION_KEY))
            and item.get("status") in _HELD
            and isinstance(lease, int)
            and lease <= now_ms
        )


class A2ARunReclaimConsumer:
    """Claim, resume or fail the expired-lease A2A runs of one host replica."""

    def __init__(
        self,
        *,
        authority: Any,
        candidates: Callable[[], Iterable[ReclaimCandidate]],
        clock: Callable[[], float],
        work_engine_provider: Callable[[], Any],
        sleep: Callable[[float], Any] = asyncio.sleep,
        emit: Callable[[str, Mapping[str, Any]], None] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        lease_ttl_s: float | None = None,
    ) -> None:
        low, high = _POLL_RANGE
        if not low <= poll_interval_s <= high:
            raise ValueError(f"poll_interval_s must be within [{low}, {high}]")
        from .authority import _RUN_LEASE_SECONDS

        self._authority = authority
        self._candidates = candidates
        self._clock = clock
        self._engine = work_engine_provider
        self._sleep = sleep
        self._emit = emit or (lambda _event, _fields: None)
        self._poll = poll_interval_s
        self._lease_ttl_s = lease_ttl_s or _RUN_LEASE_SECONDS
        self._task: asyncio.Task[None] | None = None

    async def reclaim(self, candidate: ReclaimCandidate) -> ReclaimOutcome:
        """Claim one item by name and resume it, or fail it on a refused delegation."""
        item = candidate.item
        item_id = str(item["work_item_id"])
        engine = self._engine()
        with use_session(candidate.session):
            try:
                claim = await asyncio.to_thread(
                    work_items.claim_specific,
                    engine,
                    item_id,
                    token=f"arbora-a2a-reclaim:{item_id}",
                    lease_ttl_s=self._lease_ttl_s,
                    now=self._clock(),
                )
            except Exception as exc:  # noqa: BLE001 - only the delegation refusal is an answer
                if not delegation_refused(exc):
                    raise
                await self._fail(engine, item)
                self._emit("a2a_run_delegation_refused", {"work_item_id": item_id})
                return ReclaimOutcome(item_id, "failed")
            if claim is None:
                return ReclaimOutcome(item_id, "skipped")
            try:
                await self._authority.resume_reclaimed(
                    engine, dict(item), claim, session=candidate.session
                )
            except RunDelegationRefused as refused:
                # The claim names no delegation of the run's owner: no step runs.
                await self._fail(engine, item)
                self._emit(
                    "a2a_run_delegation_refused",
                    {"work_item_id": item_id, "reason": refused.reason},
                )
                return ReclaimOutcome(item_id, "failed")
            return ReclaimOutcome(item_id, "resumed")

    async def _fail(self, engine: Any, item: Mapping[str, Any]) -> None:
        """Final ``failed`` event first, then ``CancelWorkItem`` (the commit)."""
        item_id = str(item["work_item_id"])
        metadata = item.get("metadata") or {}
        log = self._authority.events
        if log is not None and not metadata.get(FAILURE_KEY):
            await log.publish(
                item_id,
                {
                    "task_id": item_id.removeprefix(_PREFIX),
                    "context_id": str(metadata.get("a2a_context_id") or ""),
                    "kind": "status-update",
                    "state": "failed",
                    "final": True,
                },
                claim=None,
                metadata={FAILURE_KEY: DELEGATION_INVALID},
            )
        await asyncio.to_thread(
            work_items.cancel_work_item,
            engine,
            item_id,
            reason=DELEGATION_INVALID,
            now=self._clock(),
        )
        if log is not None:
            log.notify(item_id)

    async def run_once(self) -> list[ReclaimOutcome]:
        """Reclaim every expired-lease item now visible; one failure never stops the rest."""
        outcomes: list[ReclaimOutcome] = []
        candidates = await asyncio.to_thread(lambda: list(self._candidates()))
        for candidate in candidates:
            try:
                outcomes.append(await self.reclaim(candidate))
            except Exception:  # noqa: BLE001 - the next poll retries this item
                self._emit(
                    "a2a_run_reclaim_failed",
                    {"work_item_id": str(candidate.item.get("work_item_id"))},
                )
        return outcomes

    async def run(self) -> None:
        """Poll until cancelled."""
        while True:
            try:
                await self.run_once()
            except Exception:  # noqa: BLE001 - the next poll retries
                self._emit("a2a_run_reclaim_poll_failed", {})
            await self._sleep(self._poll)

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())
        return self._task

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


class A2ARunReclaimService:
    """The consumer as a host background service: ``run(should_stop)`` until stopped.

    The host's background thread owns the loop; this holds no state beyond the consumer,
    whose every decision reads the engine (ARBORA-A2A-R001.2.2).
    """

    def __init__(self, consumer: A2ARunReclaimConsumer) -> None:
        self.consumer = consumer

    async def run(self, should_stop: Callable[[], bool]) -> None:
        while not should_stop():
            try:
                await self.consumer.run_once()
            except Exception:  # noqa: BLE001 - the next poll retries
                self.consumer._emit("a2a_run_reclaim_poll_failed", {})
            await self.consumer._sleep(self.consumer._poll)


def build_run_reclaim_service(
    *,
    authority: Any,
    process_session: Callable[[], Any],
    clock: Callable[[], float],
    sleep: Callable[[float], Any] = asyncio.sleep,
    emit: Callable[[str, Mapping[str, Any]], None] | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> A2ARunReclaimService:
    """Compose the reclaim consumer for the served host.

    The candidates are the engine's own WorkItem list for the tenant the host serves.
    The tenant session is asked of the process authority at each poll (never kept), and
    the engine's list under it is the only source of what is expired.
    """
    candidates = ExpiredLeaseCandidates(
        authority.work_engine, lambda: (process_session(),), clock=clock
    )
    return A2ARunReclaimService(
        A2ARunReclaimConsumer(
            authority=authority,
            candidates=candidates,
            clock=clock,
            work_engine_provider=authority.work_engine,
            sleep=sleep,
            emit=emit,
            poll_interval_s=poll_interval_s,
        )
    )
