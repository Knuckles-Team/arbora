"""The installed local anonymous text path end to end (ARBORA-IDENTITY-R004.9.2, L6).

``ARBORA_QUAL_ANON_BASE_URL`` is an installed host in local anonymous mode. The test sends one
text message over the served A2A JSON-RPC door, reads the task, cancels a second one, and
asserts the answer is text only with no tool call.
"""

from __future__ import annotations

import json
import uuid

import pytest
from _identity_live import request, require


def _rpc(base: str, method: str, params: dict) -> dict:
    status, _, body = request(
        base,
        "POST",
        "/a2a",
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
    )
    assert status == 200
    return json.loads(body)


@pytest.mark.spec("ARBORA-IDENTITY-R004.9.2")
def test_full_served_path() -> None:
    base = require("ARBORA_QUAL_ANON_BASE_URL")["ARBORA_QUAL_ANON_BASE_URL"].rstrip("/")
    message = {
        "messageId": str(uuid.uuid4()),
        "role": "user",
        "parts": [{"kind": "text", "text": "reply with the word ready"}],
    }
    sent = _rpc(base, "message/send", {"message": message})
    task = sent["result"]
    assert task["id"] and "error" not in sent
    assert all(part.get("kind") == "text" for part in _flatten(task))
    second = _rpc(
        base,
        "message/send",
        {
            "message": {**message, "messageId": str(uuid.uuid4())},
            "configuration": {"blocking": False},
        },
    )["result"]
    cancelled = _rpc(base, "tasks/cancel", {"id": second["id"]})
    assert cancelled["result"]["status"]["state"] in {"canceled", "completed"}


def _flatten(task: dict) -> list[dict]:
    parts: list[dict] = []
    for artifact in task.get("artifacts", []):
        parts.extend(artifact.get("parts", []))
    return parts
