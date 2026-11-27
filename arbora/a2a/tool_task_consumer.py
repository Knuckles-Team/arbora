"""Served tool-task consumer (ARBORA-A2A-R002.2.3.2, .3.3, .3.4, .3.5).

The consumer polls the engine WorkItem queue ``tool_task`` and runs each ready
carrier through :func:`~arbora.a2a.tool_task.run_tool_task`. It holds no state
that correctness depends on: the WorkItem record, its lease fence and its
``effect_phase`` live in the engine.

* Before any effect it proves the original caller still holds authority
  (``resolve_original_caller``); a revoked or expired caller commits a failed
  result with zero effects and the delivery is acknowledged.
* It marks the effect started under the lease fence before the dispatcher runs.
  A reclaimed item whose effect is started and unresolved is never executed
  again. The consumer asks the dispatcher to look the job id up, commits a
  proven outcome, and otherwise leaves the item open and unacknowledged and
  emits ``tool_task_uncertain`` once per reconcile interval.
* A second delivery of one job finds the item settled (returns the stored
  receipt reference) or claimed (skipped); the connector sees one execution.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .caller_authority import DelegationReader, resolve_original_caller
from .tool_task import (
    TERMINAL_WORK_ITEM_STATES,
    ToolTaskCarrier,
    ToolTaskDispatcher,
    ToolTaskRequest,
    ToolTaskResult,
    ToolTaskRun,
    WorkItemFence,
    WorkItemLeaseLost,
    run_tool_task,
    tool_task_work_item_id,
)

__all__ = [
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_RECONCILE_INTERVAL_S",
    "EffectFence",
    "ToolTaskConsumer",
    "ToolTaskQueue",
    "ToolTaskReconciler",
    "UNAVAILABLE",
]

#: The ``/ready`` state of a host that has no engine client to consume from.
UNAVAILABLE = "UNAVAILABLE"
DEFAULT_POLL_INTERVAL_S = 2.0
DEFAULT_RECONCILE_INTERVAL_S = 30.0
_POLL_RANGE = (0.1, 60.0)
_RECONCILE_RANGE = (5.0, 3600.0)


@runtime_checkable
class ToolTaskQueue(Protocol):
    """The engine WorkItem queue ``tool_task``, read as verified carriers."""

    def ready(self) -> Sequence[ToolTaskCarrier]: ...

    def acknowledge(self, carrier: ToolTaskCarrier) -> None: ...


@runtime_checkable
class EffectFence(WorkItemFence, Protocol):
    """A fence that also keeps the engine's uncertain-effect markers."""

    def mark_effect_started(self, item_id: str, claim: str) -> None: ...


@runtime_checkable
class ToolTaskReconciler(ToolTaskDispatcher, Protocol):
    """A dispatcher that can ask the connector for a job's outcome by job id."""

    def lookup(
        self, job_id: str, *, request: ToolTaskRequest
    ) -> ToolTaskResult | None: ...


def _bounded(name: str, value: float, bounds: tuple[float, float]) -> float:
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"{name} must be within [{low}, {high}]")
    return value


@dataclass(frozen=True, slots=True)
class _Delivery:
    carrier: ToolTaskCarrier
    run: ToolTaskRun


class _ClaimFence:
    """Remember the claim token so the effect marker rides the live fence."""

    def __init__(self, inner: EffectFence) -> None:
        self._inner = inner
        self.claims: dict[str, str] = {}

    def get(self, item_id: str) -> Mapping[str, Any] | None:
        return self._inner.get(item_id)

    def claim(self, item_id: str) -> str | None:
        claim = self._inner.claim(item_id)
        if claim is not None:
            self.claims[item_id] = claim
        return claim

    def require_current(self, item_id: str, claim: str) -> None:
        self._inner.require_current(item_id, claim)

    def commit(self, item_id: str, claim: str, *, succeeded: bool, ref: str) -> None:
        self._inner.commit(item_id, claim, succeeded=succeeded, ref=ref)

    def mark_started(self, item_id: str) -> None:
        self._inner.mark_effect_started(item_id, self.claims[item_id])


class _GuardedDispatcher:
    """Authorize the original caller, then mark the effect, then execute."""

    def __init__(
        self,
        inner: ToolTaskDispatcher,
        fence: _ClaimFence,
        read_delegation: DelegationReader,
    ) -> None:
        self._inner = inner
        self._fence = fence
        self._read = read_delegation

    def resolve(
        self, request_ref: str, *, job_id: str, tenant: str, session_id: str
    ) -> ToolTaskRequest:
        request = self._inner.resolve(
            request_ref, job_id=job_id, tenant=tenant, session_id=session_id
        )
        # A refusal here commits a failed result before any effect starts.
        resolve_original_caller(request, read_delegation=self._read)
        return request

    def execute(
        self,
        request: ToolTaskRequest,
        *,
        job_id: str,
        require_current: Callable[[], None],
    ) -> ToolTaskResult:
        require_current()
        self._fence.mark_started(tool_task_work_item_id(job_id))
        return self._inner.execute(
            request, job_id=job_id, require_current=require_current
        )


class ToolTaskConsumer:
    """Poll, deliver and reconcile tool tasks for one host replica."""

    def __init__(
        self,
        *,
        queue: ToolTaskQueue,
        fence: EffectFence,
        dispatcher: ToolTaskReconciler,
        read_delegation: DelegationReader,
        clock: Callable[[], float],
        sleep: Callable[[float], Any] = asyncio.sleep,
        emit: Callable[[str, Mapping[str, Any]], None] | None = None,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        reconcile_interval_s: float = DEFAULT_RECONCILE_INTERVAL_S,
        scope: Callable[[], AbstractContextManager[Any]] = nullcontext,
    ) -> None:
        self._queue = queue
        self._fence = _ClaimFence(fence)
        self._dispatcher = dispatcher
        self._guarded = _GuardedDispatcher(dispatcher, self._fence, read_delegation)
        self._clock = clock
        self._sleep = sleep
        self._emit = emit or (lambda _event, _fields: None)
        self._poll = _bounded("poll_interval_s", poll_interval_s, _POLL_RANGE)
        self._reconcile = _bounded(
            "reconcile_interval_s", reconcile_interval_s, _RECONCILE_RANGE
        )
        self._scope = scope
        self._uncertain_at: dict[str, float] = {}
        self._task: asyncio.Task[None] | None = None

    def deliver(self, carrier: ToolTaskCarrier) -> ToolTaskRun:
        """Run or reconcile one delivery; the caller acknowledges on ``acknowledge``."""
        item_id = tool_task_work_item_id(carrier.job_id)
        record = self._fence.get(item_id)
        if (
            record is not None
            and record.get("status") not in TERMINAL_WORK_ITEM_STATES
            and record.get("effect_phase") == "started"
        ):
            return self._reconcile_item(carrier, item_id, record)
        run = run_tool_task(
            carrier,
            fence=self._fence,
            dispatcher=self._guarded,
            now=self._clock(),
        )
        if run.outcome == "skipped" and record is not None:
            # A settled item answers with the stored receipt, never a new effect.
            ref = record.get("result_ref") or record.get("error_ref") or ""
            return ToolTaskRun("skipped", str(ref))
        return run

    def _reconcile_item(
        self, carrier: ToolTaskCarrier, item_id: str, record: Mapping[str, Any]
    ) -> ToolTaskRun:
        del record
        claim = self._fence.claim(item_id)
        if claim is None:
            return ToolTaskRun("fenced")
        try:
            request = self._dispatcher.resolve(
                carrier.payload_ref,
                job_id=carrier.job_id,
                tenant=carrier.tenant,
                session_id=carrier.session_id,
            )
            proven = self._dispatcher.lookup(carrier.job_id, request=request)
            if (
                isinstance(proven, ToolTaskResult)
                and proven.job_id == carrier.job_id
                and proven.request_ref == carrier.payload_ref
                and proven.principal == request.principal
                and proven.outcome in {"completed", "failed"}
            ):
                self._fence.require_current(item_id, claim)
                self._fence.commit(
                    item_id,
                    claim,
                    succeeded=proven.outcome == "completed",
                    ref=proven.receipt_ref,
                )
                self._uncertain_at.pop(item_id, None)
                return ToolTaskRun(proven.outcome, proven.receipt_ref)
        except WorkItemLeaseLost:
            return ToolTaskRun("fenced")
        except Exception:  # noqa: BLE001 - an unreadable outcome stays uncertain
            self._emit("tool_task_lookup_failed", {"job_id": carrier.job_id})
        self._announce_uncertain(carrier, item_id)
        return ToolTaskRun("indeterminate", "tool_task_execution_uncertain")

    def _announce_uncertain(self, carrier: ToolTaskCarrier, item_id: str) -> None:
        now = self._clock()
        last = self._uncertain_at.get(item_id)
        if last is None or now - last >= self._reconcile:
            self._uncertain_at[item_id] = now
            self._emit("tool_task_uncertain", {"job_id": carrier.job_id})

    def run_once(self) -> list[_Delivery]:
        """Deliver every carrier now visible; acknowledge only settled ones."""
        deliveries: list[_Delivery] = []
        for carrier in self._queue.ready():
            run = self.deliver(carrier)
            if run.acknowledge:
                self._queue.acknowledge(carrier)
            deliveries.append(_Delivery(carrier, run))
        return deliveries

    def _poll_once(self) -> list[_Delivery]:
        """One poll under the host's session scope, on the polling thread."""
        with self._scope():
            return self.run_once()

    async def run(self) -> None:
        """Poll until cancelled; one failed poll never ends the consumer."""
        while True:
            try:
                await asyncio.to_thread(self._poll_once)
            except Exception:  # noqa: BLE001 - the next poll retries
                self._emit("tool_task_poll_failed", {})
            await self._sleep(self._poll)

    def start(self) -> asyncio.Task[None]:
        """Start the one consumer task of this replica."""
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self.run())
        return self._task

    async def stop(self) -> None:
        """Stop polling. Leases are not renewed, so claims lapse to the engine."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
