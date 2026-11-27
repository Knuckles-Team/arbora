"""The durable A2A event cursor (ARBORA-A2A-R001.2, ARBORA-A2A-R001.4.1)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from arbora.a2a.event_log import (
    EVENT_NAMESPACE,
    EVENT_SEQ_KEY,
    A2AEventFenceLost,
    A2AEventGap,
    A2AEventLog,
    A2AEventStreams,
)
from arbora.core.settings import RuntimeConfig
from arbora.security.session import use_session
from tests.a2a.test_operation_bridge import session
from tests.test_agents_run_end_to_end import FakeEngine

ITEM = "workitem:orchestrator:a2a-" + "a" * 64
TASK = "a2a-" + "a" * 64


class StepClock:
    """A controlled clock: time moves only when told; waits end on the signal."""

    def __init__(self) -> None:
        self.now = 1_000
        self.waits = 0
        self.parked = asyncio.Event()

    def now_ms(self) -> int:
        return self.now

    async def wait(self, signal: asyncio.Event, timeout_s: float) -> None:
        self.waits += 1
        self.parked.set()
        await signal.wait()


@pytest.fixture
def bound() -> Iterator[None]:
    with use_session(session(frozenset({"kg:read", "kg:write"}))):
        yield


def _engine() -> FakeEngine:
    engine = FakeEngine()
    engine.work_items.submit(
        {"work_item_id": ITEM, "kind": "orchestrator_task", "metadata": {"a": 1}}
    )
    return engine


def _claim(engine: FakeEngine, worker: str = "w1") -> dict[str, Any]:
    claimed = engine.work_items.claim({"work_item_id": ITEM, "worker_ref": worker})
    return {**claimed, "tenant": "tenant:one"}


def _draft(state: str = "working", *, final: bool = False, **extra: Any) -> dict:
    return {
        "task_id": TASK,
        "context_id": "a2a-context-" + "b" * 64,
        "kind": "status-update",
        "state": state,
        "final": final,
        **extra,
    }


def _log(engine: FakeEngine, clock: StepClock | None = None) -> A2AEventLog:
    return A2AEventLog(
        lambda: engine, clock=clock or StepClock(), config=RuntimeConfig()
    )


@pytest.mark.spec("ARBORA-A2A-R001.2")
def test_event_seq_advances_under_the_lease_and_two_writers_one_wins(
    bound: None,
) -> None:
    engine = _engine()
    stale = _claim(engine, "w1")
    # The lease is reclaimed: the first claim is now a stale fence.
    engine.work_items.rows[ITEM]["status"] = "ready"
    fresh = _claim(engine, "w2")
    assert fresh["fencing_token"] > stale["fencing_token"]
    log_a, log_b = _log(engine), _log(engine)

    async def scenario() -> tuple[int, list[dict[str, Any]]]:
        with pytest.raises(A2AEventFenceLost):
            await log_a.publish(ITEM, _draft("working"), claim=stale)
        sequence = await log_b.publish(ITEM, _draft("working"), claim=fresh)
        with pytest.raises(A2AEventFenceLost):
            await log_a.publish(ITEM, _draft("completed", final=True), claim=stale)
        item = engine.work_items.get(tenant="tenant:one", work_item_id=ITEM)
        assert item is not None
        return sequence, await log_b.read(item, 0)

    sequence, records = asyncio.run(scenario())
    # The orphan write of the stale writer never became committed: the winner's
    # event holds sequence 1 and the committed sequence is exactly 1.
    assert sequence == 1
    assert engine.work_items.rows[ITEM]["metadata"][EVENT_SEQ_KEY] == 1
    assert [r["event_seq"] for r in records] == [1]
    assert records[0]["fence"] == fresh["fencing_token"]


@pytest.mark.spec("ARBORA-A2A-R001.2")
def test_concurrent_writers_under_one_fence_publish_exactly_one_event(
    bound: None,
) -> None:
    engine = _engine()
    claim = _claim(engine)
    log_a, log_b = _log(engine), _log(engine)
    # Both writers read the same committed sequence before either writes.
    barrier = threading.Barrier(2, timeout=10)
    read = engine.work_items.get

    def read_together(**request: Any) -> Any:
        item = read(**request)
        barrier.wait()
        return item

    engine.work_items.get = read_together  # type: ignore[method-assign]

    async def scenario() -> list[Any]:
        return list(
            await asyncio.gather(
                log_a.publish(ITEM, _draft("working"), claim=claim),
                log_b.publish(ITEM, _draft("working"), claim=claim),
                return_exceptions=True,
            )
        )

    results = asyncio.run(scenario())
    assert sorted(type(r).__name__ for r in results) == ["A2AEventFenceLost", "int"]
    assert engine.work_items.rows[ITEM]["metadata"][EVENT_SEQ_KEY] == 1


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.4.1")
def test_replay_from_a_cursor_returns_exactly_the_tail_after_a_restart(
    bound: None,
) -> None:
    engine = _engine()
    claim = _claim(engine)

    async def scenario() -> list[list[int]]:
        writer = _log(engine)
        await writer.publish(ITEM, _draft("working"), claim=claim)
        await writer.publish(
            ITEM,
            {**_draft(), "kind": "artifact-update", "artifact": {"artifactId": "x"}},
            claim=claim,
        )
        await writer.publish(ITEM, _draft("completed", final=True), claim=claim)
        reader = _log(engine)  # a fresh process: no in-memory counters
        item = engine.work_items.get(tenant="tenant:one", work_item_id=ITEM)
        assert item is not None
        return [
            [r["event_seq"] for r in await reader.read(item, after)]
            for after in (0, 1, 2, 3)
        ]

    assert asyncio.run(scenario()) == [[1, 2, 3], [2, 3], [3], []]


@pytest.mark.spec("ARBORA-A2A-R001.2", "ARBORA-A2A-R001.4.1")
def test_a_cursor_ahead_of_the_log_and_a_missing_record_refuse_as_a_gap(
    bound: None,
) -> None:
    engine = _engine()
    claim = _claim(engine)

    async def scenario() -> None:
        log = _log(engine)
        await log.publish(ITEM, _draft("working"), claim=claim)
        await log.publish(ITEM, _draft("completed", final=True), claim=claim)
        item = engine.work_items.get(tenant="tenant:one", work_item_id=ITEM)
        assert item is not None
        with pytest.raises(A2AEventGap):
            await log.read(item, 3)
        del engine.kv[(EVENT_NAMESPACE, f"{ITEM}/000000000001")]
        with pytest.raises(A2AEventGap):
            await log.read(item, 0)

    asyncio.run(scenario())


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_every_published_event_passes_the_redactor(bound: None) -> None:
    engine = _engine()
    claim = _claim(engine)
    secret_key = "sk-" + "A1b2C3d4E5f6G7h8I9j0"
    bearer = "Bearer " + "abcdEFGH12345678"

    async def scenario() -> None:
        await _log(engine).publish(
            ITEM,
            {
                **_draft(),
                "kind": "artifact-update",
                "artifact": {
                    "artifactId": "r",
                    "parts": [{"kind": "text", "text": f"key {secret_key} {bearer}"}],
                    "metadata": {"token": "plain-looking-value"},
                },
            },
            claim=claim,
        )

    asyncio.run(scenario())
    raw = b"".join(value for (ns, _), (value, _v) in engine.kv.items() if ns)
    stored = json.loads(raw)
    assert secret_key not in raw.decode() and "abcdEFGH12345678" not in raw.decode()
    assert "plain-looking-value" not in raw.decode()
    assert stored["artifact"]["parts"][0]["text"] == "key [REDACTED] [REDACTED]"
    assert stored["artifact"]["metadata"]["token"] == "[REDACTED]"


class _Authority:
    """The owner check: only the caller's own task resolves."""

    def __init__(self, engine: FakeEngine, owned: bool = True) -> None:
        self._engine, self._owned = engine, owned

    async def owned_item(self, task_id: str) -> dict[str, Any] | None:
        if not self._owned or task_id != TASK:
            return None
        return self._engine.work_items.get(tenant="tenant:one", work_item_id=ITEM)


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_a_foreign_or_unknown_task_opens_no_stream(bound: None) -> None:
    engine = _engine()

    async def scenario() -> list[object]:
        log = _log(engine)
        return [
            await A2AEventStreams(
                log, _Authority(engine, owned=False)
            ).resume_from_cursor(TASK, 0),
            await A2AEventStreams(log, _Authority(engine)).resume_from_cursor(
                "a2a-" + "c" * 64, 0
            ),
        ]

    assert asyncio.run(scenario()) == [None, None]


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_a_stream_goes_live_on_a_signal_and_ends_on_the_terminal_event(
    bound: None,
) -> None:
    engine = _engine()
    claim = _claim(engine)
    clock = StepClock()

    async def scenario() -> tuple[list[int], int]:
        log = _log(engine, clock)
        streams = A2AEventStreams(log, _Authority(engine))
        await log.publish(ITEM, _draft("working"), claim=claim)
        stream = await streams.resume_from_cursor(TASK, 0)
        assert stream is not None
        seen = [r["event_seq"] for r in await stream.batch()]
        assert await stream.batch() == [] and not stream.finished
        waiter = asyncio.create_task(stream.wait())
        await clock.parked.wait()
        assert not waiter.done()
        await log.publish(ITEM, _draft("completed", final=True), claim=claim)
        await waiter
        # The final event is written but the WorkItem is not terminal yet.
        assert await stream.batch() == [] and not stream.finished
        engine.work_items.commit_result(work_item_id=ITEM, outcome="succeeded")
        seen += [r["event_seq"] for r in await stream.batch()]
        stream.close()
        assert stream.finished
        return seen, clock.waits

    assert asyncio.run(scenario()) == ([1, 2], 1)


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_card_advertises_streaming_only_when_the_authority_is_wired() -> None:
    from arbora.a2a.composition import build_a2a_service
    from arbora.a2a.routing import OrchestratorA2ARouter
    from arbora.a2a.service import A2AService
    from arbora.run_origin import RunOriginRegistry
    from tests.a2a.test_application import Authority

    engine = _engine()
    wired = build_a2a_service(
        engine_provider=lambda: engine,
        origins=RunOriginRegistry(),
        config=RuntimeConfig(),
    )
    bare = A2AService(
        authority=Authority(),
        router=OrchestratorA2ARouter(lambda: engine),
        config=RuntimeConfig(),
    )
    assert wired.agent_card("http://x/a2a").capabilities.streaming is True
    assert bare.agent_card("http://x/a2a").capabilities.streaming is False
