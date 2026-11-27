"""A served A2A route over scripted admission, routing and authority doubles."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.service import A2AService
from arbora.api.domain.a2a.models import A2ARouteDecision, A2ATask, A2ATaskStatus
from tests.a2a.test_application import Authenticator

HEADERS = {"Authorization": "Bearer verified", "Idempotency-Key": "send-key"}


class CapabilityRouter:
    """Routes to one agent whose capability record names a profile and class."""

    def __init__(self, **capability: Any) -> None:
        self.capability = {
            "model_profile_ref": "profile:one",
            "task_class": "task:chat",
            **capability,
        }

    async def route(self, message: Any, *, context_budget_tokens: int | None) -> Any:
        return A2ARouteDecision(
            agent_name="meristem-expert", selection_mode="test", **self.capability
        )


class RecordingAuthority:
    def __init__(self) -> None:
        self.dispatched: list[dict[str, Any]] = []

    async def dispatch(self, **kwargs: Any) -> A2ATask:
        self.dispatched.append(kwargs)
        return A2ATask(
            id="a2a-" + "1" * 64,
            context_id="a2a-context-" + "2" * 64,
            status=A2ATaskStatus(state="submitted"),
        )


def client(service: A2AService) -> TestClient:
    return TestClient(
        create_a2a_application(service=service, authenticator=Authenticator())
    )


def send(http: TestClient, budget: int | None = 4_096) -> Any:
    params: dict[str, Any] = {
        "message": {
            "role": "user",
            "parts": [{"kind": "text", "text": "do work"}],
            "messageId": "message-1",
        }
    }
    if budget is not None:
        params["contextBudgetTokens"] = budget
    return http.post(
        "/a2a",
        headers=HEADERS,
        json={"jsonrpc": "2.0", "id": "s", "method": "message/send", "params": params},
    )
