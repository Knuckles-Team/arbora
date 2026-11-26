"""Streaming and resubscribe over the served HTTP A2A route (ARBORA-A2A-R001.4.1)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest
from meristem import host_ports

from arbora.core.settings import runtime_config
from arbora.mcp_server import runtime as mcp_runtime
from tests.test_agents_run_end_to_end import FakeEngine, engine  # noqa: F401

_TOKENS = {"fixture-verified": "tenant:one", "fixture-foreign": "tenant:two"}


def _served(
    monkeypatch: pytest.MonkeyPatch, gate: asyncio.Event, started: asyncio.Event
) -> Any:
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from arbora.security import request_identity
    from arbora.security.actor_context import ActorContext, ActorType

    monkeypatch.setitem(
        runtime_config._overrides, "auth_jwt_audience", "arbora-fixture"
    )
    monkeypatch.setitem(
        runtime_config._overrides, "kg_policy_version", "policy:fixture"
    )

    async def credential_owner(credential: str) -> ActorContext:
        return ActorContext(
            actor_id="person:one",
            actor_type=ActorType.HUMAN,
            tenant_id=_TOKENS[credential],
            authenticated=True,
            roles=("kg:read", "kg:write"),
            credential_expires_at=int(time.time()) + 3600,
        )

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)

    async def gated_model(messages: Any, info: Any) -> ModelResponse:
        started.set()
        await gate.wait()
        return ModelResponse(parts=[TextPart("the reply, key sk-" + "Zz9" * 8)])

    host_ports.register_model_provider(lambda _id: FunctionModel(gated_model))
    _args, mcp, _middlewares = mcp_runtime._build_server(bootstrap=False)
    return mcp.http_app()


def _headers(token: str = "fixture-verified", key: str = "") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "X-Request-Id": "request:served"}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": method, "method": method, "params": params}


def _frames(text: str) -> list[tuple[str | None, dict[str, Any]]]:
    """The ``(id, json-rpc body)`` of each server-sent event."""
    frames = []
    for block in filter(None, text.split("\n\n")):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        frames.append((lines.get("id"), json.loads(lines["data"])))
    return frames


@pytest.mark.spec("ARBORA-A2A-R001.4.1", "ARBORA-A2A-R001.2.1")
def test_stream_disconnect_and_resubscribe_from_a_mid_cursor_get_exactly_the_rest(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    app = _served(monkeypatch, gate, started)
    message = {
        "role": "user",
        "messageId": "message-stream",
        "parts": [{"kind": "text", "text": "stream me an answer"}],
        "metadata": {"agent_name": "helper"},
    }

    async def scenario() -> dict[str, Any]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            card = (
                await client.get("/.well-known/agent-card.json", headers=_headers())
            ).json()
            # Stream, then disconnect while the run is still working.
            stream = asyncio.create_task(
                client.post(
                    "/a2a",
                    headers=_headers(key="stream-1"),
                    json=_rpc("message/stream", {"message": message}),
                )
            )
            await started.wait()
            stream.cancel()
            with pytest.raises(asyncio.CancelledError):
                await stream
            # The caller lost its response, so it finds its task by listing.
            listed = (
                await client.post(
                    "/a2a", headers=_headers(), json=_rpc("tasks/list", {})
                )
            ).json()
            task_id = listed["result"]["tasks"][0]["id"]
            gate.set()
            rest = await client.post(
                "/a2a",
                headers=_headers(),
                json=_rpc("tasks/resubscribe", {"id": task_id, "afterSeq": 1}),
            )
            whole = await client.post(
                "/a2a",
                headers=_headers(),
                json=_rpc("tasks/resubscribe", {"id": task_id, "afterSeq": 0}),
            )
            done = await client.post(
                "/a2a",
                headers=_headers(),
                json=_rpc("tasks/resubscribe", {"id": task_id, "afterSeq": 3}),
            )
            ahead = await client.post(
                "/a2a",
                headers=_headers(),
                json=_rpc("tasks/resubscribe", {"id": task_id, "afterSeq": 9}),
            )
            absent = await client.post(
                "/a2a",
                headers=_headers(),
                json=_rpc("tasks/resubscribe", {"id": "a2a-" + "d" * 64}),
            )
            foreign = await client.post(
                "/a2a",
                headers=_headers("fixture-foreign"),
                json=_rpc("tasks/resubscribe", {"id": task_id}),
            )
            return {
                "card": card,
                "rest": rest,
                "whole": whole,
                "done": done,
                "ahead": ahead,
                "absent": absent,
                "foreign": foreign,
            }

    out = asyncio.run(scenario())

    assert out["card"]["capabilities"]["streaming"] is True
    whole = _frames(out["whole"].text)
    assert [frame_id for frame_id, _ in whole] == ["1", "2", "3"]
    events = [body["result"] for _, body in whole]
    assert [e["metadata"]["event_seq"] for e in events] == [1, 2, 3]
    assert [e["kind"] for e in events] == [
        "status-update",
        "artifact-update",
        "status-update",
    ]
    assert events[0]["status"]["state"] == "working" and events[0]["final"] is False
    assert events[2]["status"]["state"] == "completed" and events[2]["final"] is True
    reply = events[1]["artifact"]["parts"][0]["text"]
    assert reply == "the reply, key [REDACTED]"  # the redactor ran before delivery
    # Resubscribing from the mid cursor yields exactly the rest, each once.
    assert _frames(out["rest"].text) == whole[1:]
    assert out["done"].status_code == 200 and out["done"].text == ""
    assert out["ahead"].status_code == 409
    assert out["ahead"].json()["error"]["code"] == -32010
    # An unknown task and another tenant's task answer identically.
    assert out["absent"].status_code == out["foreign"].status_code == 404
    assert out["absent"].json()["error"] == out["foreign"].json()["error"]
    assert out["absent"].json()["error"]["code"] == -32001


@pytest.mark.spec("ARBORA-A2A-R001.4.1")
def test_message_stream_sends_then_streams_the_events_in_order(
    engine: FakeEngine,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate, started = asyncio.Event(), asyncio.Event()
    gate.set()
    app = _served(monkeypatch, gate, started)
    message = {
        "role": "user",
        "messageId": "message-stream-2",
        "parts": [{"kind": "text", "text": "answer in one go"}],
        "metadata": {"agent_name": "helper"},
    }

    async def scenario() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as client:
            return await client.post(
                "/a2a",
                headers=_headers(key="stream-2"),
                json=_rpc("message/stream", {"message": message}),
            )

    response = asyncio.run(scenario())
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = _frames(response.text)
    # The admitted task first, then each durable event under its event_seq.
    assert frames[0][0] is None and frames[0][1]["result"]["kind"] == "task"
    assert [frame_id for frame_id, _ in frames[1:]] == ["1", "2", "3"]
    assert frames[-1][1]["result"]["final"] is True
