"""The worker runs a loop body after the run-now receipt (ARBORA-OPS-R022.2.13)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.fleet import loop_worker as lw
from arbora.fleet.loop_worker import TickState
from arbora.pin_lag import medulla as pin_lag

CLAIMED = {
    "task_id": "task-1",
    "queue": "loop_run",
    "kind": "loop_run",
    "payload_ref": "loop-9",
    "attempt": 2,
    "lease_expires_at_ms": 1,
}


class Engine:
    """Answers each engine method by name and records every send."""

    def __init__(self, **answers: Any) -> None:
        self.answers = {
            "AdmitWorkerTask": {"Granted": {"grant_id": "g1", "slots": 1}},
            "TaskQueueClaim": CLAIMED,
            "Loops": {"instance": {"state": "running", "objective_digest": "d" * 8}},
            **answers,
        }
        self.sent: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, method: str, params: dict[str, Any]) -> Any:
        self.sent.append((method, params))
        answer = self.answers.get(method, {})
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def methods(self) -> list[str]:
        return [method for method, _ in self.sent]


class Body:
    def __init__(self, result: lw.LoopBodyResult | Exception, beats: int = 0) -> None:
        self.result, self.beats = result, beats
        self.runs: list[lw.LoopRun] = []

    async def run(
        self, run: lw.LoopRun, heartbeat: Callable[[], Awaitable[None]]
    ) -> lw.LoopBodyResult:
        self.runs.append(run)
        for _ in range(self.beats):
            await heartbeat()
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture(autouse=True)
def served(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(lw, "require_methods", lambda *_names: None)


def worker(engine: Engine, body: Body) -> lw.LoopWorker:
    return lw.LoopWorker(engine, body, worker_id="w1", host_iri="host-a")


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_a_claimed_run_task_runs_the_body_renews_and_acknowledges() -> None:
    engine, body = Engine(), Body(lw.LoopBodyResult("Succeeded", "done"), beats=2)
    tick = asyncio.run(worker(engine, body).run_once())
    assert tick == lw.WorkerTick(TickState.RAN, "task-1", "loop-9", "Succeeded")
    assert body.runs == [lw.LoopRun("loop-9", "task-1", 2, "d" * 8)]
    assert engine.methods() == [
        "WorkerRegister",
        "AdmitWorkerTask",
        "TaskQueueClaim",
        "Loops",
        "TaskQueueRenew",
        "TaskQueueRenew",
        "TaskQueueAck",
    ]
    register = engine.sent[0][1]["registration"]
    assert register["lanes_supported"] == ["loop_run"]
    ack = engine.sent[-1][1]
    assert (ack["task_id"], ack["outcome"], ack["detail"]) == (
        "task-1",
        "Succeeded",
        "done",
    )


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_no_grant_or_no_task_runs_nothing() -> None:
    refused = Engine(AdmitWorkerTask={"Refused": "ADMIT_HELD_FOR_RESERVE"})
    body = Body(lw.LoopBodyResult("Succeeded"))
    assert asyncio.run(worker(refused, body).run_once()).state == "refused"
    assert "TaskQueueClaim" not in refused.methods()
    idle = Engine(TaskQueueClaim=None)
    assert asyncio.run(worker(idle, body).run_once()).state == "idle"
    assert body.runs == [] and "TaskQueueAck" not in idle.methods()


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_a_foreign_queue_or_unrunnable_loop_never_reaches_the_body() -> None:
    body = Body(lw.LoopBodyResult("Succeeded"))
    foreign = Engine(TaskQueueClaim={**CLAIMED, "queue": "ingest"})
    tick = asyncio.run(worker(foreign, body).run_once())
    assert tick.outcome == "Retry" and foreign.sent[-1][1]["outcome"] == "Retry"
    paused = Engine(Loops={"instance": {"state": "paused"}})
    tick = asyncio.run(worker(paused, body).run_once())
    assert tick.outcome == "Failed" and "paused" in paused.sent[-1][1]["detail"]
    assert body.runs == []


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_a_failing_body_is_acknowledged_failed_by_name_never_swallowed() -> None:
    engine = Engine()
    tick = asyncio.run(worker(engine, Body(RuntimeError("boom"))).run_once())
    assert tick.outcome == "Failed"
    assert engine.sent[-1][1]["detail"] == "RuntimeError"


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_a_live_worker_registration_is_benign_and_any_other_refusal_is_not() -> None:
    body = Body(lw.LoopBodyResult("Succeeded"))
    live = Engine(WorkerRegister=RuntimeError("WORKER_ALREADY_REGISTERED: live"))
    assert asyncio.run(worker(live, body).run_once()).state == "ran"
    broken = Engine(WorkerRegister=RuntimeError("ACCESS_DENIED"))
    with pytest.raises(RuntimeError, match="ACCESS_DENIED"):
        asyncio.run(worker(broken, body).run_once())


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_a_pin_without_the_task_queue_refuses_unavailable_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.undo()
    engine = Engine()
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(worker(engine, Body(lw.LoopBodyResult("Succeeded"))).run_once())
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "WorkerRegister" and engine.sent == []


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_the_runner_ticks_back_to_back_then_waits_and_a_failed_tick_retries() -> None:
    waits: list[float] = []
    ticks: list[str] = []

    class Scripted:
        script = ["ran", "ran", "idle", "boom", "idle"]

        async def run_once(self) -> lw.WorkerTick:
            step = self.script.pop(0)
            ticks.append(step)
            if step == "boom":
                raise RuntimeError("engine down")
            return lw.WorkerTick(step)  # type: ignore[arg-type]

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        return len(waits) == 3

    runner = lw.LoopWorkerRunner(Scripted(), interval_s=7.0, wait=wait)  # type: ignore[arg-type]
    runner._loop()
    assert ticks == ["ran", "ran", "idle", "boom", "idle"]
    assert waits == [7.0, 7.0, 7.0]


@pytest.mark.spec("ARBORA-OPS-R022.2.13")
def test_the_host_starts_the_worker_only_when_a_loop_body_is_bound() -> None:
    from arbora.mcp_server.served_background import _with_loop_worker

    unbound, startup = _with_loop_worker({"x": 1}, object(), "g", lambda t: t, object)
    assert unbound == {"x": 1} and startup is None
    body = Body(lw.LoopBodyResult("Succeeded"))
    bound, startup = _with_loop_worker(
        {"loop_body": body}, object(), "g", lambda t: t, object
    )
    assert startup is not None and isinstance(
        bound["loop_worker_runner"], lw.LoopWorkerRunner
    )
