"""A run interrupted mid-run is reclaimed and resumed (ARBORA-A2A-R001.2, R001.2.2).

The host dies after two of three steps. A restarted host (new authority, new event
log, new executor over the same engine) reclaims the expired lease, resumes from
``EngineHostCheckpoint`` under the run's delegation, and the client resubscribes
from its cursor. A revoked delegation fails the run with a final ``failed`` event.
Time is the injected clock; nothing sleeps.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import agent_runs
from arbora.a2a.authority import _RUN_LEASE_SECONDS, WorkItemA2AAuthority
from arbora.a2a.checkpoint import EngineHostCheckpoint
from arbora.a2a.event_log import A2AEventLog, A2AEventStreams
from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.a2a.run_delegation import EngineRunDelegations
from arbora.a2a.run_reclaim import A2ARunReclaimConsumer, ExpiredLeaseCandidates
from arbora.run_origin import RunOriginRegistry
from arbora.security.session import use_session
from tests.a2a._agent_run_delegation import (
    DelegationStore,
    ReclaimWorkItems,
    install_agent_run_models,
)
from tests.a2a.test_checkpoint_store import Engine as CheckpointEngine
from tests.a2a.test_event_log import StepClock
from tests.a2a.test_operation_bridge import session
from tests.test_agents_run_end_to_end import FakeEngine

STEPS = ("fetch", "write", "notify")
#: A fixed epoch instant: the test clock never reads the wall clock.
START = 1_900_000_000.0


class HostDied(BaseException):
    """The host process is gone: nothing after this point runs or commits."""


class SteppedExecutor:
    """A run of three side-effecting steps that checkpoints after each one."""

    def __init__(self, checkpoints: CheckpointEngine, die_after: str | None) -> None:
        self.checkpoints = checkpoints
        self.die_after = die_after
        self.effects: list[str] = []
        self.requests: list[Any] = []
        self.sessions: list[Any] = []

    async def execute_agent(self, request: Any, *, session: Any) -> Any:
        self.requests.append(request)
        self.sessions.append(session)
        owner = agent_runs.current_native_run(request)
        assert owner is not None
        port = EngineHostCheckpoint(self.checkpoints, owner)
        done: list[str] = json.loads(await port.restore() or "[]")
        for step in STEPS:
            if step in done:
                continue
            self.effects.append(step)
            done.append(step)
            await port.save(json.dumps(done))
            if step == self.die_after:
                raise HostDied()
        return SimpleNamespace(
            run_id=request.run_id, output="all steps done", usage=None
        )


class Host:
    """One host process: its own authority, event log and executor."""

    def __init__(
        self,
        engine: FakeEngine,
        store: DelegationStore,
        checkpoints: CheckpointEngine,
        die_after: str | None,
        clock: Any,
        delegation_reader: Any = "store",
    ) -> None:
        self.executor = SteppedExecutor(checkpoints, die_after)
        self.log = A2AEventLog(lambda: engine, clock=StepClock())
        self.authority = WorkItemA2AAuthority(
            lambda: engine,
            origins=RunOriginRegistry(),
            executor=self.executor,
            events=self.log,
            delegations=EngineRunDelegations(
                store.send, clock=clock, lease_s=_RUN_LEASE_SECONDS
            ),
            delegation_reader=store.read
            if delegation_reader == "store"
            else delegation_reader,
            clock=clock,
        )


class Clock:
    """The injected clock: it moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def lenient_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decode the checkpoint double's answers without the engine's full work-item view."""
    import medulla.generated.coordination as coordination

    def get(result: Any) -> Any:
        payload = result.payload if hasattr(result, "payload") else result
        return None if payload is None else SimpleNamespace(**payload)

    monkeypatch.setattr(coordination, "decode_get_run_checkpoint", get)


def _world(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, ...]:
    install_agent_run_models(monkeypatch)
    store = DelegationStore()
    engine = FakeEngine()
    engine.work_items = ReclaimWorkItems(store)
    return engine, store, CheckpointEngine(), Clock()


def _message() -> A2AMessage:
    return A2AMessage(
        role="user", parts=[A2ATextPart(text="do three steps")], message_id="m-1"
    )


async def _interrupted_run(host: Host) -> str:
    """Admit a run and kill the host after its second step; answer the task id."""
    with pytest.raises(HostDied):
        await host.authority.dispatch(
            message=_message(),
            idempotency_key="resume-1",
            decision=A2ARouteDecision(agent_name="helper", selection_mode="named"),
        )
    [item] = host.authority._engine()[1].work_items.rows.values()
    return str(item["work_item_id"]).removeprefix("workitem:orchestrator:")


async def _projected(host: Host, task_id: str) -> str | None:
    """The ``tasks/get`` state of the caller's task."""
    item = await host.authority.owned_item(task_id)
    if item is None:
        return None
    return str(host.authority._project(item, task_id=task_id).status.state)


def _consumer(host: Host, engine: FakeEngine, clock: Clock, who: Any) -> Any:
    return A2ARunReclaimConsumer(
        authority=host.authority,
        candidates=ExpiredLeaseCandidates(lambda: engine, lambda: [who], clock=clock),
        clock=clock,
        work_engine_provider=lambda: engine,
    )


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.2.2")
def test_run_resumes_after_restart_with_same_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    who = session(frozenset({"kg:read", "kg:write", "identity:delegate"}))

    async def scenario() -> dict[str, Any]:
        with use_session(who):
            first = Host(engine, store, checkpoints, "write", clock)
            task_id = await _interrupted_run(first)
            item_id = f"workitem:orchestrator:{task_id}"
            row = engine.work_items.rows[item_id]
            # The delegation was recorded at admission and the item names it.
            delegation_id = row["metadata"]["delegation_id"]
            record = store.records[delegation_id]
            assert record["kind"] == "agent_run" and record["run_id"] == task_id
            assert record["expires_at"] > row["lease_expires_at_ms"]
            before = row["metadata"]["a2a_event_seq"]
            # Before the lease lapses nothing is reclaimed.
            second = Host(engine, store, checkpoints, None, clock)
            consumer = _consumer(second, engine, clock, who)
            assert await consumer.run_once() == []
            clock.now = START + _RUN_LEASE_SECONDS + 1
            outcomes = await consumer.run_once()
            streams = A2AEventStreams(second.log, second.authority)
            whole = await streams.resume_from_cursor(task_id, 0)
            rest = await streams.resume_from_cursor(task_id, before)
            assert whole is not None and rest is not None
            whole_events = await whole.batch()
            rest_events = await rest.batch()
            task = await _projected(second, task_id)
            return {
                "outcomes": outcomes,
                "first": first.executor.effects,
                "second": second.executor.effects,
                "before": before,
                "whole": whole_events,
                "rest": rest_events,
                "state": task,
                "status": engine.work_items.rows[item_id]["status"],
            }

    out = asyncio.run(scenario())

    assert [o.outcome for o in out["outcomes"]] == ["resumed"]
    # No side effect repeats: the second host performs only the missing step.
    assert out["first"] == ["fetch", "write"]
    assert out["second"] == ["notify"]
    assert out["status"] == "succeeded" and out["state"] == "completed"
    # The cursor is gap-free across the restart and `working` is sent once.
    seqs = [e["event_seq"] for e in out["whole"]]
    assert seqs == list(range(1, len(seqs) + 1))
    states = [e["state"] for e in out["whole"] if e["kind"] == "status-update"]
    assert states == ["working", "completed"]
    assert out["whole"][-1]["final"] is True
    # A client that saw the first event gets exactly the remaining ones.
    assert out["rest"] == out["whole"][out["before"] :]


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.2.2")
def test_revoked_delegation_fails_the_run_with_a_final_failed_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    who = session(frozenset({"kg:read", "kg:write", "identity:delegate"}))

    async def scenario() -> dict[str, Any]:
        with use_session(who):
            first = Host(engine, store, checkpoints, "write", clock)
            task_id = await _interrupted_run(first)
            item_id = f"workitem:orchestrator:{task_id}"
            store.revoke(engine.work_items.rows[item_id]["metadata"]["delegation_id"])
            second = Host(engine, store, checkpoints, None, clock)
            consumer = _consumer(second, engine, clock, who)
            clock.now = START + _RUN_LEASE_SECONDS + 1
            outcomes = await consumer.run_once()
            again = await consumer.run_once()
            stream = await A2AEventStreams(
                second.log, second.authority
            ).resume_from_cursor(task_id, 0)
            assert stream is not None
            events = await stream.batch()
            task = await _projected(second, task_id)
            return {
                "outcomes": outcomes,
                "again": again,
                "effects": second.executor.effects,
                "events": events,
                "finished": stream.finished,
                "state": task,
                "cancels": engine.work_items.cancels,
                "now_ms": int(clock.now * 1000),
            }

    out = asyncio.run(scenario())

    assert [o.outcome for o in out["outcomes"]] == ["failed"]
    assert out["again"] == []  # the item is terminal: nothing is reclaimed twice
    assert out["effects"] == []  # no step ran under the revoked delegation
    final = out["events"][-1]
    assert final["state"] == "failed" and final["final"] is True
    assert [e["state"] for e in out["events"]] == ["working", "failed"]
    assert out["finished"] is True and out["state"] == "failed"
    # CancelWorkItem finished the item on the injected clock.
    [cancel] = out["cancels"]
    assert cancel["now_ms"] == out["now_ms"]


def _reclaim_after_expiry(
    monkeypatch: pytest.MonkeyPatch,
    tamper: Any,
    *,
    reader: Any = "store",
    scopes: frozenset[str] = frozenset({"kg:read", "kg:write", "identity:delegate"}),
) -> dict[str, Any]:
    """Interrupt a run, let ``tamper(engine, store, item_id)`` change its delegation, reclaim."""
    engine, store, checkpoints, clock = _world(monkeypatch)
    who = session(scopes)

    async def scenario() -> dict[str, Any]:
        with use_session(who):
            first = Host(engine, store, checkpoints, "write", clock)
            task_id = await _interrupted_run(first)
            item_id = f"workitem:orchestrator:{task_id}"
            tamper(engine, store, item_id)
            second = Host(engine, store, checkpoints, None, clock, reader)
            consumer = _consumer(second, engine, clock, who)
            clock.now = START + _RUN_LEASE_SECONDS + 1
            outcomes = await consumer.run_once()
            stream = await A2AEventStreams(
                second.log, second.authority
            ).resume_from_cursor(task_id, 0)
            assert stream is not None
            events = await stream.batch()
            return {
                "outcomes": [o.outcome for o in outcomes],
                "effects": second.executor.effects,
                "requests": second.executor.requests,
                "sessions": second.executor.sessions,
                "states": [e["state"] for e in events if e["kind"] == "status-update"],
                "status": engine.work_items.rows[item_id]["status"],
            }

    return asyncio.run(scenario())


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.2.2")
def test_reclaim_resumes_under_the_delegation_of_the_run_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _reclaim_after_expiry(monkeypatch, lambda *_: None)

    assert out["outcomes"] == ["resumed"]
    assert out["effects"] == ["notify"]
    # The resumed run's tools are bounded by the delegation's operations.
    [request] = out["requests"]
    assert set(request.allowed_tools or ()) <= {"agent:helper"}


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.2.2")
def test_reclaim_refuses_a_delegation_of_another_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def foreign(engine: Any, store: DelegationStore, item_id: str) -> None:
        store.reassign(
            engine.work_items.rows[item_id]["metadata"]["delegation_id"], "person:two"
        )

    out = _reclaim_after_expiry(monkeypatch, foreign)

    assert out["outcomes"] == ["failed"]
    assert (
        out["effects"] == []
    )  # no step ran under a delegation the owner does not hold
    assert out["states"] == ["working", "failed"]
    assert out["status"] == "cancelled"


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.2.2")
def test_reclaim_refuses_a_claim_that_names_no_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def anonymous_claim(engine: Any, store: DelegationStore, item_id: str) -> None:
        claim = engine.work_items.claim

        def without(request: dict[str, Any]) -> dict[str, Any]:
            return {**claim(request), "delegation_id": None}

        engine.work_items.claim = without

    out = _reclaim_after_expiry(monkeypatch, anonymous_claim)

    assert out["outcomes"] == ["failed"]
    assert out["effects"] == []
    assert out["status"] == "cancelled"


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.2.2")
def test_reclaim_refuses_when_the_host_cannot_read_delegations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _reclaim_after_expiry(monkeypatch, lambda *_: None, reader=None)

    assert out["outcomes"] == ["failed"]
    assert out["effects"] == []


@pytest.mark.spec("ARBORA-A2A-R001.2.2.1")
def test_reclaimed_run_sends_the_delegation_claim_as_the_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _reclaim_after_expiry(monkeypatch, lambda *_: None)

    assert out["outcomes"] == ["resumed"]
    [acting] = out["sessions"]
    claims = acting.engine_verified_context()
    assert claims["delegation_id"] == acting.delegation_id
    assert claims["delegation_id"].startswith("a2a-run:")
    # The envelope still names the host as the principal; the engine resolves the owner.
    assert claims["principal"] == "person:one"


@pytest.mark.spec("ARBORA-A2A-R001.2.2.1")
def test_a_first_run_carries_no_delegation_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    who = session(frozenset({"kg:read", "kg:write", "identity:delegate"}))

    async def scenario() -> Any:
        with use_session(who):
            host = Host(engine, store, checkpoints, "write", clock)
            await _interrupted_run(host)
            return host.executor.sessions

    [first] = asyncio.run(scenario())
    assert first.delegation_id is None
    assert "delegation_id" not in first.engine_verified_context()


@pytest.mark.spec("ARBORA-A2A-R001.2.2.1")
def test_reclaim_refuses_a_host_without_the_delegate_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _reclaim_after_expiry(
        monkeypatch, lambda *_: None, scopes=frozenset({"kg:read", "kg:write"})
    )

    assert out["outcomes"] == ["failed"]
    assert out["effects"] == []
    assert out["sessions"] == []
    assert out["status"] == "cancelled"


@pytest.mark.spec("ARBORA-A2A-R001.2.2.1")
def test_reclaim_refuses_on_a_release_without_the_acting_delegation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def pinned_claims(*_: Any) -> None:
        from tests.a2a._agent_run_delegation import install_pinned_claims

        install_pinned_claims(monkeypatch)

    out = _reclaim_after_expiry(monkeypatch, pinned_claims)

    assert out["outcomes"] == ["failed"]
    assert out["effects"] == []
    assert out["sessions"] == []
    assert out["status"] == "cancelled"
