"""Final program scenario: the web route and a messaging channel agree (ARBORA-ACCEPTANCE-R006.8).

The browser route asks the served ``eg.query.CrossSourceQuestion`` operation over HTTP.
The channel runs the real inbound handler of the supervised messaging router: its bound
turn handler asks the same served operation and the reply goes back through the backend.
The scenario compares text, citations and coverage field by field.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from arbora.messaging import planner_handler
from arbora.messaging.host_ports import bind_host_ports, reset_host_ports
from arbora.messaging.models import EventType, InboundEvent, Message
from arbora.messaging.service import MessagingService
from arbora.testing.scenario_stack import (
    CHANNEL_ANSWER_DIFFERS,
    ScenarioRefused,
    ScenarioRun,
    ScenarioStack,
    check_same_answer,
)
from tests.messaging._fakes import FakeMessagingBackend
from tests.messaging.conftest import FakeTimer
from tests.scenarios.conftest import scenario_stack  # noqa: F401
from tests.scenarios.test_p5_cross_source import QUESTION, ask, route_cross_source

QUESTIONS = (QUESTION,)


def _event(text: str) -> InboundEvent:
    message = Message(id="m1", content=text, channel_id="42", platform="telegram")
    return InboundEvent(
        event_type=EventType.MESSAGE,
        platform="telegram",
        channel_id="42",
        user_id="7",
        message=message,
        content=text,
    )


class _Service:
    def _resolve_engine(self) -> str:
        return "engine"

    def deliver_reply(self, *_args: Any) -> bool:
        return False


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.8")
async def test_web_and_messaging_return_the_same_cited_answer(
    scenario_stack: ScenarioStack,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = scenario_stack
    route_cross_source(stack, monkeypatch)
    monkeypatch.setenv("MESSAGING_ALLOWED_SENDERS", "telegram:7")
    monkeypatch.setenv("MESSAGING_SENDER_PRINCIPALS", '{"telegram:7": "principal-7"}')
    monkeypatch.setattr(
        "arbora.messaging.sender_identity._exchange_for_principal",
        lambda principal: f"token-for-{principal}",
    )
    monkeypatch.setattr(MessagingService, "instance", lambda _engine=None: _Service())

    async def turn(_engine: Any, text: str, *, session: Any) -> str:
        # The channel asks the served operation; it never runs the question itself.
        answer = await asyncio.to_thread(ask, stack)
        return json.dumps(answer, sort_keys=True)

    reset_host_ports()
    bind_host_ports(turn_handler=turn)
    timer = FakeTimer()
    handler = await planner_handler.create_planner_handler(
        clock=timer.clock, sleep=timer.sleep
    )
    replied = asyncio.Event()

    class Backend(FakeMessagingBackend):
        async def reply_to(self, *args: Any, **kwargs: Any) -> Any:
            result = await super().reply_to(*args, **kwargs)
            replied.set()
            return result

    backend = Backend()
    try:
        for question in QUESTIONS:
            await handler(_event(question), backend)
            replied.clear()
            await timer.advance(3.0)
            # The turn asks the served operation on a worker thread; wait for the reply.
            await asyncio.wait_for(replied.wait(), timeout=30)
            web = await asyncio.to_thread(ask, stack)
            channel = json.loads(backend.replies[-1][2])
            check_same_answer(web, channel)
            assert web["citations"] and web["coverage"] == {"excluded": []}
    finally:
        reset_host_ports()
    assert len(backend.replies) == len(QUESTIONS)
    run = ScenarioRun(
        scenario_id="p5-vision-questions",
        phase="P5",
        result="passed",
        commit="0" * 40,
        requirement_ids=("ARBORA-ACCEPTANCE-R006.8",),
    )
    assert run.engine_record()["record"]["scenario_id"] == "p5-vision-questions"


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.8")
def test_a_channel_answer_that_differs_in_any_field_fails_the_scenario() -> None:
    web = {"text": "t", "citations": ["fact:1"], "coverage": {"excluded": []}}
    check_same_answer(web, dict(web))
    for changed in (
        {**web, "text": "other"},
        {**web, "citations": []},
        {**web, "coverage": {"excluded": [{"source": "s", "reason": "R"}]}},
    ):
        with pytest.raises(ScenarioRefused) as refused:
            check_same_answer(web, changed)
        assert refused.value.code == CHANNEL_ANSWER_DIFFERS
    with pytest.raises(ScenarioRefused):
        check_same_answer({"text": "t"}, {"text": "t"})
