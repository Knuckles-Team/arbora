"""Scheduled fleet smoke sweep (ARBORA-FLEET-R053).

One read-only probe per admitted connector, through the served ``fleet.call``
path as a service principal that holds only ``mcp:discover`` and the read-only
fleet scopes. A probe is a named tool with no required arguments whose
annotations carry ``readOnlyHint: true``; the sweep calls nothing else. Each run
produces one :class:`SmokeRow` per connector. The alert fires once when a
connector that passed the previous run now fails. Arbora keeps no durable state
here: the previous outcomes and the row sink are handed in by the host.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "DEFAULT_INTERVAL_S",
    "FleetSmokeService",
    "SmokeRow",
    "pick_probe",
    "run_sweep",
    "smoke_interval_s",
]

DEFAULT_INTERVAL_S = 900
_MIN_INTERVAL_S = 60
_MAX_INTERVAL_S = 86_400
_DETAIL_MAX = 200
Outcome = Literal["ok", "error", "refused", "no_probe"]
FAILED: frozenset[str] = frozenset({"error", "refused"})


class SmokeRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    server: str
    tool: str | None
    outcome: Outcome
    error_code: str | None = None
    detail: str = Field(default="", max_length=_DETAIL_MAX)


class SweepFleet(Protocol):
    def servers(self) -> Sequence[str]: ...

    def tools(self, server: str) -> Sequence[Mapping[str, Any]]: ...

    async def call(self, server: str, tool: str) -> Any: ...


class SweepRefused(Exception):
    """The served path refused the call; ``code`` is the refusal code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def smoke_interval_s(value: Any = None) -> int:
    """``ARBORA_FLEET_SMOKE_INTERVAL_S``: default 900, bounds [60, 86400]."""
    if value is None:
        return DEFAULT_INTERVAL_S
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    return max(_MIN_INTERVAL_S, min(_MAX_INTERVAL_S, seconds))


def pick_probe(tools: Sequence[Mapping[str, Any]]) -> str | None:
    """The first read-only tool that needs no argument, by name order."""
    for tool in sorted(tools, key=lambda t: str(t.get("name", ""))):
        schema = tool.get("inputSchema") or {}
        annotations = tool.get("annotations") or {}
        if annotations.get("readOnlyHint") is True and not schema.get("required"):
            return str(tool["name"])
    return None


def _detail(text: Any) -> str:
    return " ".join(str(text).split())[:_DETAIL_MAX]


async def run_sweep(
    fleet: SweepFleet,
    *,
    previous: Mapping[str, str],
    record: Callable[[Sequence[SmokeRow]], None],
    alert: Callable[[SmokeRow], None],
) -> tuple[SmokeRow, ...]:
    """Probe every connector once; record the rows; alert on a new failure."""
    rows: list[SmokeRow] = []
    for server in fleet.servers():
        tool = pick_probe(fleet.tools(server))
        if tool is None:
            rows.append(SmokeRow(server=server, tool=None, outcome="no_probe"))
            continue
        try:
            await fleet.call(server, tool)
            row = SmokeRow(server=server, tool=tool, outcome="ok")
        except SweepRefused as refused:
            row = SmokeRow(
                server=server,
                tool=tool,
                outcome="refused",
                error_code=refused.code,
                detail=_detail(refused.detail),
            )
        except Exception as exc:  # noqa: BLE001 - one connector never stops the sweep
            row = SmokeRow(
                server=server,
                tool=tool,
                outcome="error",
                error_code=type(exc).__name__,
                detail=_detail(type(exc).__name__),
            )
        rows.append(row)
        if row.outcome in FAILED and previous.get(server) == "ok":
            alert(row)
    record(rows)
    return tuple(rows)


class FleetSmokeService:
    """Run the sweep when ``interval_s`` has passed on the supplied clock."""

    def __init__(
        self,
        fleet: SweepFleet,
        *,
        record: Callable[[Sequence[SmokeRow]], None],
        alert: Callable[[SmokeRow], None],
        clock: Callable[[], float],
        interval_s: Any = None,
    ) -> None:
        self._fleet = fleet
        self._record = record
        self._alert = alert
        self._clock = clock
        self.interval_s = smoke_interval_s(interval_s)
        self._next = 0.0
        self._previous: dict[str, str] = {}

    async def tick(self) -> tuple[SmokeRow, ...] | None:
        now = self._clock()
        if now < self._next:
            return None
        self._next = now + self.interval_s
        rows = await run_sweep(
            self._fleet,
            previous=self._previous,
            record=self._record,
            alert=self._alert,
        )
        self._previous = {r.server: r.outcome for r in rows}
        return rows
