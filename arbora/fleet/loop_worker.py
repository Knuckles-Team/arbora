"""The worker that runs a loop body after the run-now receipt (ARBORA-OPS-R022.2.13).

``evolution.loops.run`` (ARBORA-OPS-R022.2.4) claims the ``Loops`` lease and
enqueues one engine task on the ``loop_run`` queue. The engine hosts no loop
body and calls no model, so this worker, a replica of the serving process,
claims that task, runs the body through one :class:`LoopBody` port and
acknowledges the outcome. The engine holds everything that matters: the task,
its lease, its attempt count and the loop record. The worker keeps no state
between ticks, so a crashed replica's task is redelivered by the engine when
its lease lapses.

Admission is the engine's: the worker registers for the ``loop_run`` lane, asks
for a slot grant and claims under it. A refused grant means back off, never try
another lane. A pinned engine without the task-queue methods refuses
``UNAVAILABLE`` naming the first missing one (``arbora.pin_lag.medulla``).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import decode
from arbora.api.ops.pin_guards import refusal
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods

__all__ = [
    "LOOP_RUN_QUEUE",
    "LoopBody",
    "LoopBodyResult",
    "LoopRun",
    "LoopWorker",
    "LoopWorkerRunner",
    "WorkerTick",
]

logger = logging.getLogger(__name__)

#: The engine queue and lane of the loop run tasks (``evolution.loops.run``).
LOOP_RUN_QUEUE = "loop_run"
#: The engine methods a tick uses, in the order it first needs them.
TASK_METHODS: tuple[str, ...] = (
    "WorkerRegister",
    "AdmitWorkerTask",
    "TaskQueueClaim",
    "TaskQueueRenew",
    "TaskQueueAck",
)
#: Loop states from which a queued run must not start.
NOT_RUNNABLE = frozenset({"paused", "cancelled", "rejected", "completed", "failed"})
_ALREADY_REGISTERED = "WORKER_ALREADY_REGISTERED"

Send = Callable[[str, dict[str, Any]], Awaitable[Any]]
Outcome = Literal["Succeeded", "Failed", "Retry"]


@dataclass(frozen=True, slots=True)
class LoopRun:
    """What the body is told about the run it executes."""

    loop_id: str
    task_id: str
    attempt: int
    objective_digest: str


@dataclass(frozen=True, slots=True)
class LoopBodyResult:
    """How a body run ended: the task outcome the engine records and a short detail."""

    outcome: Outcome
    detail: str = ""


@dataclass(frozen=True, slots=True)
class WorkerTick:
    """The outcome of one tick: ``idle`` or the task, its loop and what was acknowledged."""

    state: Literal["idle", "refused", "ran"]
    task_id: str = ""
    loop_id: str = ""
    outcome: str = ""


@runtime_checkable
class LoopBody(Protocol):
    """Runs one loop iteration. ``heartbeat`` extends the task lease while it runs."""

    async def run(
        self, run: LoopRun, heartbeat: Callable[[], Awaitable[None]]
    ) -> LoopBodyResult: ...


class LoopWorker:
    """Claims one ``loop_run`` task per tick, runs its body and acknowledges it."""

    def __init__(
        self,
        send: Send,
        body: LoopBody,
        *,
        worker_id: str,
        host_iri: str,
        cores: int = 1,
        memory_bytes: int = 1 << 28,
        lease_ms: int = 60_000,
    ) -> None:
        self._send = send
        self._body = body
        self._worker_id = worker_id
        self._host_iri = host_iri
        self._cores = cores
        self._memory = memory_bytes
        self._lease_ms = lease_ms

    async def _register(self) -> None:
        try:
            await self._send(
                "WorkerRegister",
                {
                    "registration": {
                        "worker_id": self._worker_id,
                        "host_iri": self._host_iri,
                        "cores": self._cores,
                        "memory_bytes": self._memory,
                        "lanes_supported": [LOOP_RUN_QUEUE],
                    }
                },
            )
        except Exception as error:  # only the live-worker refusal is benign
            if _ALREADY_REGISTERED not in f"{getattr(error, 'code', '')} {error}":
                raise

    async def _grant(self) -> str | None:
        answer = decode(
            await self._send(
                "AdmitWorkerTask",
                {"worker_id": self._worker_id, "requested_max": 1},
            )
        )
        granted = answer.get("Granted") if isinstance(answer, Mapping) else None
        if not isinstance(granted, Mapping) or not granted.get("slots"):
            return None
        return str(granted["grant_id"])

    async def _ack(self, task_id: str, outcome: Outcome, detail: str) -> None:
        await self._send(
            "TaskQueueAck",
            {
                "task_id": task_id,
                "worker_id": self._worker_id,
                "outcome": outcome,
                "detail": detail[:256],
            },
        )

    async def _loop_view(self, loop_id: str) -> Mapping[str, Any]:
        answer = decode(
            await self._send("Loops", {"op": {"op": "get", "loop_id": loop_id}})
        )
        instance = answer.get("instance") if isinstance(answer, Mapping) else None
        if not isinstance(instance, Mapping) or "state" not in instance:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "unexpected engine answer"}
            )
        return instance

    async def run_once(self) -> WorkerTick:
        """One tick: register, take a grant, claim, run the body, acknowledge."""
        try:
            require_methods(*TASK_METHODS)
        except PinLagUnavailable as missing:
            raise refusal(missing) from missing
        await self._register()
        grant = await self._grant()
        if grant is None:
            return WorkerTick("refused")
        claimed = decode(
            await self._send(
                "TaskQueueClaim",
                {
                    "worker_id": self._worker_id,
                    "grant_id": grant,
                    "lease_ms": self._lease_ms,
                },
            )
        )
        if not isinstance(claimed, Mapping):
            return WorkerTick("idle")
        task_id = str(claimed["task_id"])
        if claimed.get("queue") != LOOP_RUN_QUEUE:
            # Placement is the engine's: a task of another queue goes back untouched.
            await self._ack(task_id, "Retry", "not a loop run task")
            return WorkerTick("ran", task_id, "", "Retry")
        loop_id = str(claimed["payload_ref"])
        result = await self._execute(claimed, task_id, loop_id)
        await self._ack(task_id, result.outcome, result.detail)
        return WorkerTick("ran", task_id, loop_id, result.outcome)

    async def _execute(
        self, claimed: Mapping[str, Any], task_id: str, loop_id: str
    ) -> LoopBodyResult:
        async def heartbeat() -> None:
            await self._send(
                "TaskQueueRenew",
                {
                    "task_id": task_id,
                    "worker_id": self._worker_id,
                    "lease_ms": self._lease_ms,
                },
            )

        try:
            instance = await self._loop_view(loop_id)
            if instance["state"] in NOT_RUNNABLE:
                return LoopBodyResult("Failed", f"loop is {instance['state']}")
            run = LoopRun(
                loop_id=loop_id,
                task_id=task_id,
                attempt=int(claimed.get("attempt", 0)),
                objective_digest=str(instance.get("objective_digest", "")),
            )
            return await self._body.run(run, heartbeat)
        except Exception as error:  # the body's failure is the task's outcome
            logger.exception("loop run %s failed", task_id)
            return LoopBodyResult("Failed", type(error).__name__)


class LoopWorkerRunner:
    """The serving process's schedule for the loop worker.

    One daemon thread runs :meth:`LoopWorker.run_once` on a private event loop
    every ``interval_s``, back to back while tasks keep arriving. A failed tick
    is logged by name and the next one retries. ``wait(seconds)`` blocks for the
    interval and returns True once stopped, so a test drives the schedule with
    a controlled clock instead of sleeping.
    """

    def __init__(
        self,
        worker: LoopWorker,
        *,
        interval_s: float,
        wait: Callable[[float], bool] | None = None,
    ) -> None:
        self._worker = worker
        self._interval_s = interval_s
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last = WorkerTick("idle")

    async def tick(self) -> WorkerTick:
        try:
            self.last = await self._worker.run_once()
        except Exception:  # the next tick retries; the task stays leased
            logger.exception("loop worker: tick failed; the task stays queued")
        return self.last

    def _loop(self) -> None:
        import asyncio

        while not self._stop.is_set():
            if asyncio.run(self.tick()).state != "ran" and self._wait(self._interval_s):
                return

    def start(self) -> bool:
        """Start the schedule once; False when it already runs or was stopped."""
        with self._lock:
            if self._thread is not None or self._stop.is_set():
                return False
            self._thread = threading.Thread(
                target=self._loop, name="arbora-loop-worker", daemon=True
            )
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)
