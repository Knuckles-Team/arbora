"""Step usage events reach the usage ledger (ARBORA-OPS-R891).

The run event sink hands every ``step_usage`` event to :func:`forward_step_usage`, which
buffers it under its run. At run end :func:`flush_usage` sends the buffer to the engine
``Usage`` ``Append`` in batches of at most ``max_batch`` records, through the service
executor because ``usage:write`` is service-only. The tenant and principal come from the
run's own binding (:meth:`UsageLedger.bind`), never from an event field. An engine
refusal is logged and never fails the run or drops the other batches.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DEFAULT_MAX_BATCH = 500
USAGE_SCOPES = frozenset({"usage:write"})


@dataclass(slots=True)
class _Run:
    tenant: str
    principal: str
    events: list[Mapping[str, Any]] = field(default_factory=list)


def _record(
    run: _Run, run_id: str, event: Mapping[str, Any], now_ms: int
) -> dict[str, Any]:
    def count(key: str) -> int:
        value = event.get(key, 0)
        return value if type(value) is int and value >= 0 else 0

    return {
        "agent_id": str(event.get("agent_id") or ""),
        "child_id": str(event.get("child_id") or ""),
        "content_digest": str(event.get("content_digest") or ""),
        "latency_ms": count("latency_ms"),
        "model_key": str(event.get("model_key") or event.get("model") or ""),
        "operation": str(event.get("operation") or "step"),
        "outcome": "error" if event.get("outcome") == "error" else "ok",
        "principal": run.principal,
        "project_id": str(event.get("project_id") or ""),
        "recorded_at_ms": count("recorded_at_ms") or now_ms,
        "run_id": run_id,
        "schema_version": SCHEMA_VERSION,
        "tenant": run.tenant,
        "tokens_cached": count("tokens_cached"),
        "tokens_in": count("tokens_in"),
        "tokens_out": count("tokens_out"),
    }


class UsageLedger:
    """Per-run buffers of step usage; the engine holds the ledger itself."""

    def __init__(self) -> None:
        self._runs: dict[str, _Run] = {}
        self._callers: Any = None
        self._max_batch = DEFAULT_MAX_BATCH
        self._now_ms: Callable[[], int] = lambda: 0

    def configure(
        self,
        callers: Any,
        *,
        max_batch: int = DEFAULT_MAX_BATCH,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self._callers = callers
        self._max_batch = max(1, int(max_batch))
        if now_ms is not None:
            self._now_ms = now_ms

    def bind(self, run_id: str, *, tenant: str, principal: str) -> None:
        """Bind one run to the tenant and principal of its run context."""
        self._runs.setdefault(run_id, _Run(tenant=tenant, principal=principal))

    def forward(self, event: Mapping[str, Any]) -> None:
        """Buffer one ``step_usage`` event; an event of an unbound run is dropped."""
        run = self._runs.get(str(event.get("run_id")))
        if run is not None and event.get("type") == "step_usage":
            run.events.append(dict(event))

    async def flush(self, run_id: str) -> int:
        """Send the run's buffer in batches; return the number of batches accepted."""
        run = self._runs.pop(run_id, None)
        if run is None or not run.events or self._callers is None:
            return 0
        now = self._now_ms()
        records = [_record(run, run_id, event, now) for event in run.events]
        accepted = 0
        for index in range(0, len(records), self._max_batch):
            batch = records[index : index + self._max_batch]
            try:
                await self._callers.send(
                    run.tenant,
                    "Usage",
                    {"op": {"op": "append", "records": batch}},
                    scopes=USAGE_SCOPES,
                    key=f"usage:{run_id}:{index // self._max_batch}",
                )
                accepted += 1
            except Exception as exc:  # usage never fails the run
                logger.warning(
                    "usage batch %s of run %s not appended: %s %s",
                    index // self._max_batch,
                    run_id,
                    type(exc).__name__,
                    getattr(exc, "code", ""),
                )
        return accepted


LEDGER = UsageLedger()


def forward_step_usage(event: Mapping[str, Any]) -> None:
    """The run event sink's hook for one ``step_usage`` event."""
    LEDGER.forward(event)


async def flush_usage(run_id: str) -> int:
    """Append the run's buffered usage at run end."""
    return await LEDGER.flush(run_id)


__all__ = [
    "DEFAULT_MAX_BATCH",
    "LEDGER",
    "UsageLedger",
    "flush_usage",
    "forward_step_usage",
]
