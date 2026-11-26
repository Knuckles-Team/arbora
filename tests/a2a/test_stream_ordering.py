"""The final stream event follows the commit (ARBORA-A2A-R001.4.1, ARBORA-A2A-R010)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from arbora import work_items
from arbora.a2a.event_log import A2AEventStream, A2AEventStreams
from tests.a2a.test_event_cursor_served import _frames, _headers, _rpc, _served
from tests.a2a.test_event_log import (
    ITEM,
    TASK,
    StepClock,
    _Authority,
    _claim,
    _draft,
    _engine,
    _log,
    bound,  # noqa: F401 - the session fixture
)
from tests.test_agents_run_end_to_end import FakeEngine, engine  # noqa: F401


@pytest.mark.spec("ARBORA-A2A-R001.4.1", "ARBORA-A2A-R010")
def test_final_event_is_held_until_the_workitem_is_terminal(bound: None) -> None:  # noqa: F811
    engine_ = _engine()
    claim = _claim(engine_)
    clock = StepClock()

    async def scenario() -> tuple[list[int], list[int], bool]:
        log = _log(engine_, clock)
        streams = A2AEventStreams(log, _Authority(engine_))
        await log.publish(ITEM, _draft("working"), claim=claim)
        # The run writes its final event durably, before it commits the result.
        await log.publish(ITEM, _draft("completed", final=True), claim=claim)
        stream = await streams.resume_from_cursor(TASK, 0)
        assert stream is not None
        first = [r["event_seq"] for r in await stream.batch()]
        # The WorkItem is still leased: the committed final event is held back.
        assert await stream.batch() == [] and not stream.finished
        waiter = asyncio.create_task(stream.wait())
        await clock.parked.wait()
        assert not waiter.done()
        # The commit lands and wakes the stream; only then does the final arrive.
        engine_.work_items.commit_result(work_item_id=ITEM, outcome="succeeded")
        log.notify(ITEM)
        await waiter
        second = [r["event_seq"] for r in await stream.batch()]
        stream.close()
        return first, second, stream.finished

    assert asyncio.run(scenario()) == ([1], [2], True)


@pytest.mark.spec("ARBORA-A2A-R001.4.1", "ARBORA-A2A-R010")
def test_a_failed_commit_delivers_failed_and_never_the_uncommitted_completed(
    bound: None,  # noqa: F811
) -> None:
    engine_ = _engine()
    claim = _claim(engine_)

    async def scenario() -> list[tuple[int, str]]:
        log = _log(engine_)
        streams = A2AEventStreams(log, _Authority(engine_))
        await log.publish(ITEM, _draft("working"), claim=claim)
        await log.publish(ITEM, _draft("completed", final=True), claim=claim)
        # The commit did not apply: the run moves to failed through its edge.
        await log.publish(ITEM, _draft("failed", final=True), claim=claim)
        engine_.work_items.commit_result(work_item_id=ITEM, outcome="failed")
        stream = await streams.resume_from_cursor(TASK, 0)
        assert stream is not None
        seen = [(r["event_seq"], r["state"]) for r in await stream.batch()]
        stream.close()
        assert stream.finished
        return seen

    assert asyncio.run(scenario()) == [(1, "working"), (3, "failed")]


@pytest.mark.spec("ARBORA-A2A-R001.4.1", "ARBORA-A2A-R010")
def test_tasks_get_agrees_when_the_final_frame_arrives(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    gate.set()
    app = _served(monkeypatch, gate, started)
    authorities: list[Any] = []
    agreed: list[tuple[str, str]] = []
    original_open = A2AEventStreams.resume_from_cursor
    original_batch = A2AEventStream.batch

    async def open_spy(self: A2AEventStreams, task_id: str, after_seq: int = 0) -> Any:
        authorities.append((self._authority, task_id))
        return await original_open(self, task_id, after_seq)

    async def batch_spy(self: A2AEventStream) -> list[dict[str, Any]]:
        records = await original_batch(self)
        final = [r for r in records if r["final"]]
        if final:
            # The instant the final event is handed to the response, the task
            # read of the same caller must already say the same state.
            authority, task_id = authorities[-1]
            task = await authority.get(task_id)
            agreed.append((final[0]["state"], task.status.state))
        return records

    monkeypatch.setattr(A2AEventStreams, "resume_from_cursor", open_spy)
    monkeypatch.setattr(A2AEventStream, "batch", batch_spy)
    message = {
        "role": "user",
        "messageId": "message-order",
        "parts": [{"kind": "text", "text": "order me"}],
        "metadata": {"agent_name": "helper"},
    }

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            return await client.post(
                "/a2a",
                headers=_headers(key="order-1"),
                json=_rpc("message/stream", {"message": message}),
            )

    response = asyncio.run(scenario())
    assert _frames(response.text)[-1][1]["result"]["final"] is True
    assert agreed == [("completed", "completed")]


@pytest.mark.spec("ARBORA-A2A-R001.4.1", "ARBORA-A2A-R010")
def test_a_commit_that_fails_moves_the_run_to_failed_on_the_stream_and_tasks_get(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    gate.set()
    app = _served(monkeypatch, gate, started)
    real_commit = work_items.commit_result

    def commit(engine_: Any, item_id: str, claim: Any, **kwargs: Any) -> str:
        if kwargs["outcome"] == "succeeded":
            raise RuntimeError("the engine refused the commit")
        return real_commit(engine_, item_id, claim, **kwargs)

    monkeypatch.setattr(work_items, "commit_result", commit)
    message = {
        "role": "user",
        "messageId": "message-commit-fail",
        "parts": [{"kind": "text", "text": "commit me"}],
        "metadata": {"agent_name": "helper"},
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            stream = await client.post(
                "/a2a",
                headers=_headers(key="commit-fail"),
                json=_rpc("message/stream", {"message": message}),
            )
            task_id = _frames(stream.text)[0][1]["result"]["id"]
            got = await client.post(
                "/a2a", headers=_headers(), json=_rpc("tasks/get", {"id": task_id})
            )
            return stream, got

    stream, got = asyncio.run(scenario())
    events = [body["result"] for _, body in _frames(stream.text)[1:]]
    finals = [e for e in events if e.get("final")]
    assert [e["status"]["state"] for e in finals] == ["failed"]
    assert all(e["status"]["state"] != "completed" for e in events if "status" in e)
    assert got.json()["result"]["status"]["state"] == "failed"
