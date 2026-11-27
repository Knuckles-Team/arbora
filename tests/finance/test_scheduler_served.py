"""Finance schedules run on the Loops task path, once per occurrence (ARBORA-DATA-MARKET-R002)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.finance.schedule import (
    FinanceScheduleEntry,
    ScheduleRefused,
    due_occurrences,
    parse_trigger,
)
from arbora.finance.loop_body import FinanceLoopBody
from arbora.fleet.loop_worker import LoopRun
from tests.finance._receipt_engine import ReceiptMachine

CHICAGO = "America/Chicago"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(UTC)


def keys(entry: str, trigger: str, zone: str, after: str, now: str) -> list[str]:
    return [o.key for o in due_occurrences(entry, trigger, zone, at(after), at(now))]


@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
def test_the_trigger_grammar_is_one_parser() -> None:
    assert parse_trigger("daily@09:30")[0] == "daily"
    assert parse_trigger("weekly:fri@17:00")[0] == "weekly"
    assert parse_trigger("interval:60") == ("interval", 60)
    for bad in ("interval:59", "daily@24:00", "weekly:xyz@10:00", "hourly", ""):
        with pytest.raises(ScheduleRefused):
            parse_trigger(bad)
    with pytest.raises(ScheduleRefused):
        due_occurrences(
            "e",
            "daily@09:00",
            "Nowhere/Zone",
            at("2026-01-01T00:00:00+00:00"),
            at("2026-01-02T00:00:00+00:00"),
        )


@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
def test_a_skipped_wall_time_fires_once_at_the_next_valid_instant() -> None:
    # 2026-03-08 02:30 does not exist in Chicago; it fires once, an hour later in local time.
    found = due_occurrences(
        "e",
        "daily@02:30",
        CHICAGO,
        at("2026-03-07T12:00:00+00:00"),
        at("2026-03-09T12:00:00+00:00"),
    )
    skipped = [o for o in found if o.key.startswith("e:2026-03-08")]
    assert [o.key for o in skipped] == ["e:2026-03-08T02:30[America/Chicago]"]
    assert skipped[0].instant == at("2026-03-08T03:30:00-05:00")


@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
def test_a_repeated_wall_time_fires_on_its_first_pass_only() -> None:
    # 2026-11-01 01:30 happens twice in Chicago; one occurrence, at the first pass (CDT).
    found = due_occurrences(
        "e",
        "daily@01:30",
        CHICAGO,
        at("2026-10-31T12:00:00+00:00"),
        at("2026-11-02T12:00:00+00:00"),
    )
    day = [o for o in found if o.key.startswith("e:2026-11-01")]
    assert len(day) == 1
    assert day[0].instant == at("2026-11-01T01:30:00-05:00")


@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
def test_interval_slots_are_keyed_by_wall_time() -> None:
    found = keys(
        "e",
        "interval:3600",
        "UTC",
        "2026-01-01T00:00:00+00:00",
        "2026-01-01T03:00:00+00:00",
    )
    assert found == [
        "e:2026-01-01T01:00[UTC]",
        "e:2026-01-01T02:00[UTC]",
        "e:2026-01-01T03:00[UTC]",
    ]


class Host:
    def __init__(self, machine: ReceiptMachine, *, holds: bool = True) -> None:
        self.machine = machine
        self.holds = holds
        self.ran: list[str] = []
        self.marked: list[datetime] = []
        entry = FinanceScheduleEntry(
            tenant_id="t1",
            owner="o1",
            action="scan",
            timezone=CHICAGO,
            trigger="daily@09:00",
            idempotency_key="unused",
        )
        self.record = SimpleNamespace(
            entry_id="sched-1",
            grant="finance:read",
            last_run=at("2026-03-06T12:00:00+00:00"),
            entry=entry,
        )

    async def record_for(self, _loop: str) -> Any:
        return self.record

    async def owner_holds(self, _record: Any) -> bool:
        return self.holds

    def receipts(self, _record: Any) -> Any:
        return self.machine.engine()

    async def run_job(self, _record: Any, key: str) -> None:
        self.ran.append(key)

    async def mark_run(self, record: Any, last_run: datetime) -> None:
        self.marked.append(last_run)
        record.last_run = last_run


def body(host: Host, now: str) -> FinanceLoopBody:
    adapter = SimpleNamespace(
        record=host.record_for,
        owner_holds=host.owner_holds,
        receipts=host.receipts,
        run_job=host.run_job,
        mark_run=host.mark_run,
    )
    return FinanceLoopBody(adapter, clock=lambda: at(now))  # type: ignore[arg-type]


async def beat() -> None:
    return None


RUN = LoopRun(loop_id="sched-1", task_id="task-1", attempt=1, objective_digest="d")


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_dst_and_replay_run_each_occurrence_once() -> None:
    machine = ReceiptMachine()
    host = Host(machine)
    now = "2026-03-10T12:00:00+00:00"
    first = await body(host, now).run(RUN, beat)
    assert first.outcome == "Succeeded"
    assert len(host.ran) == 4  # Mar 7, 8 (the skipped-hour day), 9 and 10 at 09:00
    assert len(set(host.ran)) == 4
    # A replay of the same task, even with the last run lost, claims no key twice.
    host.record.last_run = at("2026-03-06T12:00:00+00:00")
    again = await body(host, now).run(RUN, beat)
    assert again.outcome == "Succeeded"
    assert len(host.ran) == 4
    assert host.marked[-1] == at(now)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_a_lost_owner_grant_refuses_scope_required_and_runs_nothing() -> None:
    host = Host(ReceiptMachine(), holds=False)
    result = await body(host, "2026-03-10T12:00:00+00:00").run(RUN, beat)
    assert (result.outcome, result.detail) == ("Failed", "SCOPE_REQUIRED")
    assert host.ran == [] and host.marked == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R002")
async def test_an_unknown_schedule_and_a_bad_trigger_fail_without_running() -> None:
    host = Host(ReceiptMachine())
    host.record.entry = host.record.entry.model_copy(update={"trigger": "hourly"})
    result = await body(host, "2026-03-10T12:00:00+00:00").run(RUN, beat)
    assert (result.outcome, result.detail) == ("Failed", "INVALID_ARGUMENT")
