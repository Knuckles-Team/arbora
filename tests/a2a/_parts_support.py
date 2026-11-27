"""A served A2A route that counts the tasks it creates, for file-part admission."""

from __future__ import annotations

import base64
from typing import Any

from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.service import A2AService
from arbora.api.domain.a2a.models import A2ARouteDecision
from arbora.core.settings import RuntimeConfig
from tests.a2a.test_application import Authenticator, Authority

_CONFIG = RuntimeConfig()
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 24
WEBP = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16


class CountingAuthority(Authority):
    def __init__(self) -> None:
        super().__init__()
        self.dispatched: list[Any] = []
        self.media: list[Any] = []

    async def dispatch(self, **kwargs: Any) -> Any:
        self.dispatched.append(kwargs["message"])
        self.media.append(kwargs.get("media"))
        return self.task


class AnyRouter:
    async def route(self, message: Any, *, context_budget_tokens: int | None) -> Any:
        return A2ARouteDecision(agent_name="meristem-expert", selection_mode="test")


def served(
    capability: Any = None,
    media_refs: Any = None,
    owner: str = "tenant:ann",
    config: RuntimeConfig = _CONFIG,
) -> tuple[TestClient, CountingAuthority]:
    authority = CountingAuthority()

    async def read(_message: Any, _decision: Any) -> Any:
        return capability

    service = A2AService(
        authority=authority,
        router=AnyRouter(),
        media_capability=read if capability is not None else None,
        media_refs=media_refs,
        caller_owner=lambda: owner,
        config=config,
    )
    app = create_a2a_application(
        service=service,
        authenticator=Authenticator(),
    )
    return TestClient(app), authority


def file_part(data: bytes, mime_type: str) -> dict[str, Any]:
    return {
        "kind": "file",
        "file": {"bytes": base64.b64encode(data).decode(), "mimeType": mime_type},
    }


def send(client: TestClient, parts: list[dict[str, Any]]) -> dict[str, Any]:
    return send_with_metadata(client, parts, {})


def send_with_metadata(
    client: TestClient, parts: list[dict[str, Any]], metadata: dict[str, Any]
) -> dict[str, Any]:
    response = client.post(
        "/a2a",
        headers={"Authorization": "Bearer verified", "Idempotency-Key": "send-key"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "look"}, *parts],
                    "messageId": "m1",
                    "metadata": metadata,
                }
            },
        },
    )
    body: dict[str, Any] = response.json()
    return body


def reason(body: dict[str, Any]) -> str | None:
    error = body.get("error")
    if error is None:
        return None
    assert error["code"] == -32602, body
    return str(error["data"]["reason"])
