"""ASGI live lifecycle adapter tests (injected authority, not production evidence)."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.models import A2ARouteDecision, A2ATask, A2ATaskStatus
from arbora.a2a.service import A2AService


class Auth:
    async def authenticate(self, request: Any, *, scope: str) -> None:
        if request.headers.get("Authorization") != "Bearer verified":
            raise PermissionError("refused")


class Router:
    async def route(self, message: Any, **kwargs: Any) -> A2ARouteDecision:
        return A2ARouteDecision(agent_name="arbora", selection_mode="test")


class Authority:
    def __init__(self) -> None:
        self.dispatches = 0
        self.reads: list[Any] = []
        self.task = A2ATask(
            id="work-item:" + "1" * 64,
            context_id="a2a-context-" + "2" * 64,
            status=A2ATaskStatus(state="working"),
        )

    async def streaming_supported(self) -> bool:
        return True

    async def dispatch(self, **kwargs: Any) -> A2ATask:
        assert kwargs["streaming"] is True
        self.dispatches += 1
        return self.task

    async def get(self, task_id: str) -> A2ATask | None:
        return self.task if task_id == self.task.id else None

    async def list(
        self, *, cursor: str | None = None, limit: int = 50
    ) -> tuple[list[A2ATask], str | None]:
        return [self.task], None

    async def cancel(self, task_id: str) -> A2ATask:
        raise PermissionError("not part of stream test")

    async def read_events(self, task_id: str, cursor: str | None) -> Any:
        self.reads.append((task_id, cursor))
        if task_id != self.task.id:
            raise PermissionError("foreign task")
        return {
            "work_item": {
                "status": "succeeded",
                "metadata": {
                    "a2a_context_id": self.task.context_id,
                    "a2a_run_id": "run",
                },
            },
            "events": []
            if cursor == "committed:1"
            else [
                {
                    "cursor": "committed:1",
                    "event_index": 1,
                    "event": {
                        "kind": "text_delta",
                        "content": json.dumps(
                            {
                                "delta": "actual delta",
                                "step": 0,
                                "part_index": 0,
                                "reset": True,
                            }
                        ),
                    },
                }
            ],
            "has_more": False,
        }


def test_stream_reconnect_is_read_only_and_card_uses_probe() -> None:
    authority = Authority()
    client = TestClient(
        create_a2a_application(
            service=A2AService(authority=authority, router=Router()),
            authenticator=Auth(),
        )
    )
    headers = {"Authorization": "Bearer verified", "Idempotency-Key": "once"}
    card = client.get("/.well-known/agent-card.json", headers=headers)
    assert card.json()["capabilities"]["streaming"] is True
    response = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/stream",
            "params": {
                "message": {
                    "role": "user",
                    "messageId": "m",
                    "parts": [{"kind": "text", "text": "hello"}],
                }
            },
        },
    )
    assert response.status_code == 200
    assert "id: committed:1" in response.text
    assert '"text":"actual delta"' in response.text
    replay = client.post(
        "/a2a",
        headers={**headers, "Last-Event-ID": "committed:1"},
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tasks/resubscribe",
            "params": {"id": authority.task.id},
        },
    )
    assert replay.status_code == 200 and replay.text == ""
    assert authority.dispatches == 1
    foreign = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tasks/resubscribe",
            "params": {"id": "work-item:" + "3" * 64},
        },
    )
    assert foreign.status_code == 403
    assert authority.dispatches == 1


def test_foreign_caller_is_denied_before_read_or_dispatch() -> None:
    authority = Authority()
    client = TestClient(
        create_a2a_application(
            service=A2AService(authority=authority, router=Router()),
            authenticator=Auth(),
        )
    )
    response = client.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/resubscribe",
            "params": {"id": authority.task.id},
        },
    )
    assert response.status_code == 403
    assert authority.reads == [] and authority.dispatches == 0
