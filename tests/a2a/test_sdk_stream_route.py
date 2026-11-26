"""The SDK route streams through the one event log (ARBORA-A2A-R001.4.1)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from arbora.a2a.sdk_app import AdmittedA2AApp
from tests.a2a.test_event_cursor_served import _frames, _headers, _rpc, _served
from tests.a2a.test_sdk_app import _App, _Auth
from tests.test_agents_run_end_to_end import FakeEngine, engine  # noqa: F401


class _BodyApp(_App):
    """The SDK application: records the body it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[bytes] = []

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            message = await receive()
            self.bodies.append(message["body"])
        await super().__call__(scope, receive, send)


async def _post(guard: AdmittedA2AApp, payload: dict[str, Any]) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    body = json.dumps(payload).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await guard(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        },
        receive,
        send,
    )
    return sent


@pytest.mark.spec("ARBORA-A2A-R001.4", "ARBORA-A2A-R001.4.1")
@pytest.mark.parametrize("method", ["message/stream", "tasks/resubscribe"])
def test_without_a_stream_handler_the_guard_refuses_by_type_not_500(
    method: str,
) -> None:
    app = _BodyApp()
    sent = asyncio.run(
        _post(
            AdmittedA2AApp(app, _Auth("caller")),
            {"jsonrpc": "2.0", "id": 7, "method": method, "params": {}},
        )
    )
    assert app.bodies == []  # the SDK application never saw a stream method
    assert sent[0]["status"] == 501
    error = json.loads(sent[1]["body"])
    assert error["error"]["code"] == -32010 and error["id"] == 7


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_stream_methods_go_to_the_shared_handler_and_others_keep_their_body() -> None:
    app = _BodyApp()
    handled: list[bytes] = []

    class _Response:
        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"streamed"})

    async def handler(request: Any) -> Any:
        handled.append(await request.body())
        return _Response()

    guard = AdmittedA2AApp(app, _Auth("caller"), stream_handler=handler)
    stream = {"jsonrpc": "2.0", "id": 1, "method": "message/stream", "params": {}}
    unary = {"jsonrpc": "2.0", "id": 2, "method": "message/send", "params": {}}
    sent_stream = asyncio.run(_post(guard, stream))
    sent_unary = asyncio.run(_post(guard, unary))
    assert json.loads(handled[0]) == stream and app.bodies == [
        json.dumps(unary).encode()
    ]
    assert sent_stream[1]["body"] == b"streamed" and sent_unary[1]["body"] == b"ok"


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_sdk_route_streams_through_the_one_event_log(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    gate.set()
    app = _served(monkeypatch, gate, started)
    message = {
        "role": "user",
        "messageId": "message-sdk-stream",
        "parts": [{"kind": "text", "text": "stream on the sdk route"}],
        "metadata": {"agent_name": "helper"},
    }

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            streamed = await client.post(
                "/a2a/agent",
                headers=_headers(key="sdk-stream"),
                json=_rpc("message/stream", {"message": message}),
            )
            task_id = _frames(streamed.text)[0][1]["result"]["id"]
            again = await client.post(
                "/a2a/agent",
                headers=_headers(),
                json=_rpc("tasks/resubscribe", {"id": task_id, "afterSeq": 1}),
            )
            return streamed, again

    streamed, again = asyncio.run(scenario())
    frames = _frames(streamed.text)
    assert streamed.headers["content-type"].startswith("text/event-stream")
    assert frames[0][1]["result"]["kind"] == "task"
    ids = [frame_id for frame_id, _ in frames[1:]]
    assert ids == [str(n) for n in range(1, len(ids) + 1)] and len(ids) >= 3
    assert frames[-1][1]["result"]["final"] is True

    # The resubscribe replays exactly the durable events after the cursor.
    def results(parsed: list[tuple[str | None, dict[str, Any]]]) -> list[Any]:
        return [(frame_id, body["result"]) for frame_id, body in parsed]

    assert results(_frames(again.text)) == results(frames[2:])
