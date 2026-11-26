"""Run events on the one publisher (ARBORA-A2A-R001.4.1)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
)

from arbora.a2a.application import _event_frames
from arbora.a2a.event_log import A2AEventConsumerTooSlow, A2AEventStreams
from arbora.a2a.run_events import RunEventObserver
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

SECRET = "sk-" + "A1b2C3d4E5f6G7h8I9j0"


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_text_deltas_tool_events_and_redaction_use_the_one_publisher(
    bound: None,  # noqa: F811
) -> None:
    engine = _engine()
    claim = _claim(engine)

    async def scenario() -> list[dict[str, Any]]:
        log = _log(engine)

        async def publish(draft: dict[str, Any]) -> None:
            await log.publish(ITEM, draft, claim=claim)

        observer = RunEventObserver(
            publish, task_id=TASK, context_id="a2a-context-" + "b" * 64
        )
        # A secret streamed across two deltas is never split over two events.
        await observer(
            PartStartEvent(index=0, part=TextPart("The key is " + SECRET[:9]))
        )
        await observer(
            PartDeltaEvent(index=0, delta=TextPartDelta(SECRET[9:] + " ok. "))
        )
        await observer(
            FunctionToolCallEvent(
                part=ToolCallPart("find", {"intent": "x", "token": SECRET}, "call-1")
            )
        )
        await observer(
            FunctionToolResultEvent(
                part=ToolReturnPart("find", f"found {SECRET}", "call-1")
            )
        )
        await observer(PartDeltaEvent(index=1, delta=TextPartDelta("Done.")))
        assert await observer.finish(f"The key is {SECRET} ok. Done.") is True
        item = engine.work_items.get(tenant="tenant:one", work_item_id=ITEM)
        assert item is not None
        return await log.read(item, 0)

    records = asyncio.run(scenario())
    raw = json.dumps(records)
    assert SECRET not in raw and SECRET[:9] not in raw.replace("[REDACTED]", "")
    assert [r["event_seq"] for r in records] == list(range(1, len(records) + 1))
    kinds = [
        (
            r["kind"],
            (r.get("message") or {}).get("parts", [{}])[0].get("data", {}).get("type"),
        )
        for r in records
    ]
    # Text that came before the tool call is published before the call event.
    assert kinds == [
        ("artifact-update", None),
        ("status-update", "tool-call"),
        ("status-update", "tool-result"),
        ("artifact-update", None),
        ("artifact-update", None),
    ]
    assert records[0]["artifact"]["parts"][0]["text"] == "The key is [REDACTED] ok. "
    assert (records[0]["append"], records[3]["append"]) == (False, True)
    call, result = (
        records[1]["message"]["parts"][0]["data"],
        records[2]["message"]["parts"][0]["data"],
    )
    assert call["toolName"] == "find" and call["toolCallId"] == "call-1"
    assert "[REDACTED]" in result["result"] and result["ok"] is True
    # Every event is a working, non-final update; only the run's own end is final.
    assert not any(r["final"] for r in records)
    # The last chunk closes the artifact; the whole answer equals the reply.
    assert records[-1]["last_chunk"] is True
    answer = "".join(
        r["artifact"]["parts"][0]["text"]
        for r in records
        if r["kind"] == "artifact-update"
    )
    assert answer == "The key is [REDACTED] ok. Done."


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_a_reply_that_differs_from_the_streamed_text_replaces_the_artifact(
    bound: None,  # noqa: F811
) -> None:
    published: list[dict[str, Any]] = []

    async def publish(draft: dict[str, Any]) -> None:
        published.append(draft)

    async def scenario() -> None:
        observer = RunEventObserver(publish, task_id=TASK, context_id="c")
        await observer(PartStartEvent(index=0, part=TextPart("thinking aloud ")))
        assert await observer.finish("the real answer") is True

    asyncio.run(scenario())
    assert [(d["append"], d["last_chunk"]) for d in published] == [
        (False, False),
        (False, True),
    ]
    assert published[-1]["artifact"]["parts"][0]["text"] == "the real answer"


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_a_slow_consumer_gets_a_final_error_frame(bound: None) -> None:  # noqa: F811
    engine = _engine()
    claim = _claim(engine)
    clock = StepClock()

    async def scenario() -> tuple[list[str], int]:
        log = _log(engine, clock)
        streams = A2AEventStreams(log, _Authority(engine), buffer_events=8)
        for _ in range(3):
            await log.publish(ITEM, _draft("working"), claim=claim)
        stream = await streams.resume_from_cursor(TASK, 0)
        assert stream is not None
        frames = _event_frames(None, stream, None, "rpc-1")
        received = [(await anext(frames)).decode() for _ in range(3)]
        # The consumer stalls while the run publishes far beyond the buffer.
        for _ in range(20):
            await log.publish(ITEM, _draft("working"), claim=claim)
        tail = [frame.decode() async for frame in frames]
        return received + tail, len(stream._log._waiters)  # noqa: SLF001

    frames, waiters = asyncio.run(scenario())
    assert [f.split("\n")[0] for f in frames[:3]] == ["id: 1", "id: 2", "id: 3"]
    assert len(frames) == 4  # nothing after the error frame
    error = json.loads(frames[3].removeprefix("data: "))["error"]
    assert error["code"] == -32010 and "resubscribe" in error["message"]
    assert waiters == 0  # the cut stream released its signal


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_a_catching_up_consumer_is_not_cut_and_the_read_is_bounded(
    bound: None,  # noqa: F811
) -> None:
    engine = _engine()
    claim = _claim(engine)

    async def scenario() -> list[int]:
        log = _log(engine)
        streams = A2AEventStreams(log, _Authority(engine), buffer_events=8)
        for _ in range(30):
            await log.publish(ITEM, _draft("working"), claim=claim)
        stream = await streams.resume_from_cursor(TASK, 0)
        assert stream is not None
        sizes = []
        while True:
            batch = await stream.batch()
            if not batch:
                return sizes
            sizes.append(len(batch))

    sizes = asyncio.run(scenario())
    assert sum(sizes) == 30 and max(sizes) <= 8


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_the_slow_consumer_error_is_a_gap_error() -> None:
    from arbora.a2a.event_log import A2AEventGap

    assert issubclass(A2AEventConsumerTooSlow, A2AEventGap)
