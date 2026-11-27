"""The finance schedule run body (ARBORA-DATA-MARKET-R002 and R006).

Arbora has no finance scheduler of its own. A finance schedule entry is an engine
schedule whose run reaches the same path every Loops run takes: the engine schedule
fires, arbora holds the ``Loops`` lease and enqueues a ``TaskQueueEnqueue`` task
(``evolution.loops.run``), and an arbora worker claims the task and runs this body
(``arbora.fleet.loop_worker.LoopBody``). The body computes the occurrences due since
the entry's ``last_run``, claims each by key in the engine receipt (kind
``schedule_run``) so a replay or a daylight-saving repeat runs it once, runs the job
for the entry ``action`` and records ``last_run``. A lost owner grant refuses
``SCOPE_REQUIRED`` and runs nothing. The body keeps no state between runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from arbora.finance.receipts import FinanceReceiptEngine
from arbora.finance.schedule import (
    FinanceScheduleEntry,
    ScheduleRefused,
    due_occurrences,
    record_schedule_run,
)
from arbora.fleet.loop_worker import LoopBodyResult, LoopRun

__all__ = ["FinanceLoopBody", "FinanceScheduleHost", "ScheduleRecord"]

#: The grant each action runs under (the entry names one of the three).
GRANTS = frozenset({"finance:backfill", "finance:track", "finance:read"})


class ScheduleRecord(Protocol):
    """One stored schedule entry: the entry fields, its grant and its last run."""

    entry_id: str
    grant: str
    last_run: datetime
    entry: FinanceScheduleEntry


class FinanceScheduleHost(Protocol):
    """The engine reads and the job doors the body needs; the host binds them."""

    async def record(self, loop_id: str) -> ScheduleRecord | None: ...

    async def owner_holds(self, record: ScheduleRecord) -> bool:
        """Whether the entry's owner still holds the delegated grant."""
        ...

    def receipts(self, record: ScheduleRecord) -> FinanceReceiptEngine: ...

    async def run_job(self, record: ScheduleRecord, occurrence_key: str) -> None: ...

    async def mark_run(self, record: ScheduleRecord, last_run: datetime) -> None: ...


class FinanceLoopBody:
    """Runs the due occurrences of one finance schedule entry."""

    def __init__(
        self, host: FinanceScheduleHost, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._host = host
        self._clock = clock or (lambda: datetime.now(UTC))

    async def run(
        self, run: LoopRun, heartbeat: Callable[[], Awaitable[None]]
    ) -> LoopBodyResult:
        record = await self._host.record(run.loop_id)
        if record is None:
            return LoopBodyResult("Failed", "SCHEDULE_NOT_FOUND")
        if record.grant not in GRANTS or not await self._host.owner_holds(record):
            return LoopBodyResult("Failed", "SCOPE_REQUIRED")
        entry = record.entry
        now = self._clock()
        try:
            due = due_occurrences(
                record.entry_id, entry.trigger, entry.timezone, record.last_run, now
            )
        except ScheduleRefused:
            return LoopBodyResult("Failed", "INVALID_ARGUMENT")
        ran = 0
        for occurrence in due:
            keyed = entry.model_copy(update={"idempotency_key": occurrence.key})
            if (
                await record_schedule_run(self._host.receipts(record), keyed)
                == "executed"
            ):
                await self._host.run_job(record, occurrence.key)
                ran += 1
            await heartbeat()
        await self._host.mark_run(record, now)
        return LoopBodyResult("Succeeded", f"ran {ran} of {len(due)} occurrences")
