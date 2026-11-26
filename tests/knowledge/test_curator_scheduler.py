"""The knowledge curator scheduler (ARBORA-OPS-R958)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.knowledge import curator_scheduler as cs


class FakeClock:
    """Time moves only when the scheduler waits; nothing sleeps for real."""

    def __init__(self) -> None:
        self.t = 0.0
        self.waits: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.t += seconds


class FakeCuration:
    def __init__(self, due: dict[str, bool], held: set[str] | None = None) -> None:
        self.due, self.held = due, held or set()
        self.calls: list[tuple[str, str, int | None]] = []
        self.next_epoch = 0

    async def claim(self, tenant: str, loop_id: str) -> int | None:
        self.calls.append(("claim", tenant, None))
        if tenant in self.held:
            return None
        self.next_epoch += 1
        return self.next_epoch

    async def inputs(self, tenant: str, loop_id: str, epoch: int) -> bool:
        self.calls.append(("inputs", tenant, epoch))
        return self.due[tenant]

    async def release(self, tenant: str, loop_id: str, epoch: int) -> None:
        self.calls.append(("release", tenant, epoch))


class FakeRunner:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.started: list[tuple[str, str, int, str]] = []
        self.fail = fail or set()

    async def start(
        self, tenant: str, agent_id: str, *, loop_id: str, epoch: int, key: str
    ) -> str:
        if tenant in self.fail:
            raise RuntimeError("boom")
        self.started.append((tenant, agent_id, epoch, key))
        return f"task-{tenant}"


def _scheduler(
    curation: Any, runner: Any, clock: FakeClock, tenants: tuple[str, ...]
) -> cs.CuratorScheduler:
    async def listed() -> tuple[str, ...]:
        return tenants

    return cs.CuratorScheduler(
        tenants=listed, curation=curation, runner=runner, clock=clock, tick_s=30.0
    )


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_due_tick_claims_lease_and_starts_the_agent_and_not_due_does_nothing() -> (
    None
):
    clock, runner = FakeClock(), FakeRunner()
    curation = FakeCuration({"a": True, "b": False}, held={"c"})
    scheduler = _scheduler(curation, runner, clock, ("a", "b", "c"))
    reports = await scheduler.tick()
    assert [(r.tenant, r.result) for r in reports] == [
        ("a", "started"),
        ("b", "not_due"),
        ("c", "lease_held"),
    ]
    # Due: the lease stays held for the run, and the curator is the library agent.
    assert runner.started == [
        ("a", cs.CURATOR_AGENT_ID, 1, "curation:a:knowledge-curation:1")
    ]
    # Not due: the lease is released and no run starts; a held lease asks nothing.
    assert curation.calls == [
        ("claim", "a", None),
        ("inputs", "a", 1),
        ("claim", "b", None),
        ("inputs", "b", 2),
        ("release", "b", 2),
        ("claim", "c", None),
    ]
    assert reports[0].run_id == "task-a"


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_a_failed_start_releases_the_lease_and_the_next_tenant_still_runs() -> (
    None
):
    runner = FakeRunner(fail={"a"})
    curation = FakeCuration({"a": True, "b": True})
    reports = await _scheduler(curation, runner, FakeClock(), ("a", "b")).tick()
    assert [(r.tenant, r.result) for r in reports] == [
        ("a", "failed"),
        ("b", "started"),
    ]
    assert reports[0].code == "RuntimeError"
    assert ("release", "a", 1) in curation.calls


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_run_ticks_on_the_injected_clock_and_never_sleeps_for_real() -> None:
    clock = FakeClock()
    curation = FakeCuration({"a": False})
    scheduler = _scheduler(curation, FakeRunner(), clock, ("a",))
    await scheduler.run(lambda: clock.t >= 90.0)
    assert clock.waits == [30.0, 30.0, 30.0]
    assert [c[0] for c in curation.calls].count("claim") == 3
    with pytest.raises(ValueError):
        cs.CuratorScheduler(
            tenants=lambda: None,
            curation=curation,
            runner=FakeRunner(),
            clock=clock,
            tick_s=0,  # type: ignore[arg-type]
        )


class _Cause(Exception):
    def __init__(self, detail: str) -> None:
        self.detail = detail


def _refusal(code: str, detail: str) -> EngineRefusal:
    exc = EngineRefusal(code)
    exc.__cause__ = _Cause(detail)
    return exc


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_engine_port_maps_the_lease_and_cadence_refusals() -> None:
    sent: list[tuple[str, dict[str, Any], bool]] = []
    answers: list[Any] = []

    class Callers:
        graph = "g"

        async def send(
            self,
            tenant: str,
            method: str,
            params: dict[str, Any],
            *,
            scopes: frozenset[str],
            key: str | None = None,
        ) -> Any:
            sent.append((method, params["op"], key is not None))
            assert params["op"]["graph"] == "g"
            assert scopes <= {"loops:control", "knowledge:curate"}
            answer = answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer

    port = cs.EngineCuration(Callers())  # type: ignore[arg-type]
    answers += [{"instance": {"lease": {"epoch": 7}}}]
    assert await port.claim("t", "loop") == 7
    answers += [_refusal("INVALID_ARGUMENT", "LOOP_LEASE_HELD: held by another")]
    assert await port.claim("t", "loop") is None
    answers += [_refusal("INVALID_ARGUMENT", "CURATION_NOT_DUE: not yet")]
    assert await port.inputs("t", "loop", 7) is False
    answers += [{"due": True}]
    assert await port.inputs("t", "loop", 7) is True
    answers += [_refusal("INVALID_ARGUMENT", "CURATION_LEASE_REQUIRED: none")]
    with pytest.raises(EngineRefusal):
        await port.inputs("t", "loop", 7)
    answers += [{}]
    await port.release("t", "loop", 7)
    assert sent[3][1] == {
        "op": "inputs",
        "loop_id": "loop",
        "epoch": 7,
        "force": False,
        "graph": "g",
    }
    assert sent[-1] == (
        "Loops",
        {"op": "release", "loop_id": "loop", "epoch": 7, "graph": "g"},
        True,
    )
    answers += [{"instance": {}}]
    with pytest.raises(ValueError):
        await port.claim("t", "loop")


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_agents_run_starter_carries_only_the_curate_grant_and_the_epoch() -> None:
    seen: list[tuple[str, str, dict[str, Any], str]] = []

    class Callers:
        def __init__(self, answer: Any) -> None:
            self.answer = answer

        async def invoke(
            self, tenant: str, op_id: str, params: dict[str, Any], key: str
        ) -> Any:
            seen.append((tenant, op_id, params, key))
            return self.answer

    starter = cs.AgentsRunStarter(Callers({"value": {"task_id": "task-9"}}))  # type: ignore[arg-type]
    task = await starter.start("t", "knowledge-curator", loop_id="l", epoch=3, key="k")
    assert task == "task-9"
    assert seen[0][1] == "agents.run"
    metadata = seen[0][2]["message"]["metadata"]
    assert metadata["delegated_grants"] == ["knowledge:curate"]
    assert metadata["curation"] == {"loop_id": "l", "epoch": 3}
    assert seen[0][3] == "k"

    with pytest.raises(ValueError):
        await cs.AgentsRunStarter(Callers({"value": {}})).start(  # type: ignore[arg-type]
            "t", "a", loop_id="l", epoch=1, key="k"
        )
