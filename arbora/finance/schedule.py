"""Idempotent finance-schedule run record (ARBORA-DATA-MARKET-R002, FI-04).

``FinanceScheduleEntry`` records one backfill/scan/DCA-due/explanation job's
tenant, owner, authorized action, timezone, trigger, and idempotency key, per
FR-08. ``record_schedule_run`` is the replay-safety core FI-04 tests: the
same ``(tenant_id, idempotency_key)`` pair yields exactly one ``"executed"``
outcome no matter how many times it is replayed -- a daylight-saving
transition or a restart that replays the same trigger cannot duplicate an
effect, because the key is caller-supplied and never derived from wall-clock
time. Registering these entries against ``arbora.gateway.daemon``'s
maintenance-scheduler tick (the "existing scheduler" FR-08 names) is GDM-07
follow-on work; this module owns only the dedupe record.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field

from arbora.finance.receipts import (
    FinanceReceiptEngine,
    claim_receipt,
    complete_receipt,
)

__all__ = [
    "MIN_INTERVAL_S",
    "FinanceScheduleEntry",
    "Occurrence",
    "RunOutcome",
    "ScheduleAction",
    "ScheduleRefused",
    "due_occurrences",
    "occurrence_key",
    "parse_trigger",
    "record_schedule_run",
]

ScheduleAction = Literal["backfill", "scan", "dca_due", "explain"]
RunOutcome = Literal["executed", "duplicate"]


class FinanceScheduleEntry(BaseModel):
    """One scheduled finance job occurrence."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=256)
    owner: str = Field(min_length=1, max_length=256)
    action: ScheduleAction
    timezone: str = Field(min_length=1, max_length=64, description="IANA zone.")
    trigger: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=256)


async def record_schedule_run(
    engine: FinanceReceiptEngine, entry: FinanceScheduleEntry
) -> RunOutcome:
    """Record one schedule occurrence; a replayed key never re-executes.

    FI-04: calling this twice with the same ``(tenant_id, idempotency_key)``
    -- whether from a daylight-saving transition's duplicated trigger or a
    restart's replay -- returns ``"executed"`` once and ``"duplicate"`` on
    every subsequent call, with no transition beyond the first claim.
    """

    claimed = await claim_receipt(
        engine,
        entry.tenant_id,
        "schedule_run",
        entry.idempotency_key,
        event_id=entry.idempotency_key,
        recorded_at=datetime.now(UTC).isoformat(),
    )
    if not claimed:
        return "duplicate"
    await complete_receipt(
        engine, entry.tenant_id, "schedule_run", entry.idempotency_key
    )
    return "executed"


#: The shortest interval trigger, in seconds (ARBORA-DATA-MARKET-R002).
MIN_INTERVAL_S = 60
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_CLOCK = r"([01]\d|2[0-3]):([0-5]\d)"
_DAILY = re.compile(rf"^daily@{_CLOCK}$")
_WEEKLY = re.compile(rf"^weekly:({'|'.join(_DAYS)})@{_CLOCK}$")
_INTERVAL = re.compile(r"^interval:(\d{1,9})$")


class ScheduleRefused(ValueError):
    """A bad trigger or zone; the served code is ``INVALID_ARGUMENT``."""

    code = "INVALID_ARGUMENT"


class Occurrence:
    """One due fire: the instant it runs and the key that claims it once."""

    __slots__ = ("instant", "key")

    def __init__(self, instant: datetime, key: str) -> None:
        self.instant = instant
        self.key = key

    def __repr__(self) -> str:
        return f"Occurrence({self.key!r})"


def parse_trigger(trigger: str) -> tuple[str, int | tuple[int | None, time]]:
    """The one trigger grammar: ``daily@HH:MM``, ``weekly:<mon..sun>@HH:MM``, ``interval:<s>``."""
    if (match := _DAILY.match(trigger)) is not None:
        return "daily", (None, time(int(match[1]), int(match[2])))
    if (match := _WEEKLY.match(trigger)) is not None:
        return "weekly", (_DAYS.index(match[1]), time(int(match[2]), int(match[3])))
    if (match := _INTERVAL.match(trigger)) is not None:
        seconds = int(match[1])
        if seconds < MIN_INTERVAL_S:
            raise ScheduleRefused(f"interval must be at least {MIN_INTERVAL_S} seconds")
        return "interval", seconds
    raise ScheduleRefused("unknown trigger")


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleRefused("unknown time zone") from None


def occurrence_key(entry_id: str, wall: datetime, zone: str) -> str:
    """``<entry_id>:<local wall time ISO 8601>[<zone name>]``; the wall time is nominal."""
    return (
        f"{entry_id}:{wall.replace(tzinfo=None).isoformat(timespec='minutes')}[{zone}]"
    )


def _instant(day: date, clock: time, zone: ZoneInfo) -> datetime:
    """A wall time on a day as a UTC instant. A skipped time fires once at the next
    valid instant (fold 0 reads the offset before the gap); a repeated time fires on
    its first pass."""
    return datetime.combine(day, clock, tzinfo=zone).replace(fold=0).astimezone(UTC)


def due_occurrences(
    entry_id: str, trigger: str, zone_name: str, after: datetime, now: datetime
) -> list[Occurrence]:
    """Every occurrence with ``after < instant <= now`` (UTC), oldest first."""
    _kind, spec = parse_trigger(trigger)
    zone = _zone(zone_name)
    if isinstance(spec, int):
        step = spec
        first = int(after.timestamp()) // step + 1
        last = int(now.timestamp()) // step
        found = []
        for slot in range(first, last + 1):
            instant = datetime.fromtimestamp(slot * step, UTC)
            found.append(
                Occurrence(
                    instant,
                    occurrence_key(entry_id, instant.astimezone(zone), zone_name),
                )
            )
        return found
    weekday, clock = spec
    day = after.astimezone(zone).date() - timedelta(days=1)
    end = now.astimezone(zone).date() + timedelta(days=1)
    found = []
    while day <= end:
        if weekday is None or day.weekday() == weekday:
            instant = _instant(day, clock, zone)
            if after < instant <= now:
                nominal = datetime.combine(day, clock)
                found.append(
                    Occurrence(instant, occurrence_key(entry_id, nominal, zone_name))
                )
        day += timedelta(days=1)
    return found
