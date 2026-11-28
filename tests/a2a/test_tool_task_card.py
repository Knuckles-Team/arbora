"""Tool-task Agent Card extension and approval attestation (ARBORA-A2A-R002.2.4)."""

from __future__ import annotations

from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.service import A2AService
from arbora.a2a.tool_task_card import (
    EXTENSION_URI,
    ToolTaskAttestation,
    sign_attestation,
    verify_attestation,
)
from arbora.api.domain.a2a.models import A2ATask, A2ATaskState, A2ATaskStatus
from arbora.core.settings import RuntimeConfig
from tests.a2a.test_application import Authenticator, Authority, Router

HEADERS = {"Authorization": "Bearer verified", "Idempotency-Key": "send-key"}
RECORD = "approval:record-1"
DIGEST = "a" * 64


class Signer:
    key_id = "key-1"

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()

    def sign(self, data: bytes) -> bytes:
        return self.key.sign(data)


class Admitter:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    async def admit(self, *, call: Any, message: Any, idempotency_key: str) -> Any:
        self.calls.append((call.operation, call.parameters, idempotency_key))
        task = A2ATask(
            id="a2a-" + "3" * 64,
            context_id="a2a-context-" + "4" * 64,
            status=A2ATaskStatus(state=A2ATaskState("submitted")),
        )
        return task, RECORD, DIGEST


def _client(**kwargs: Any) -> TestClient:
    service = A2AService(
        authority=Authority(), router=Router(), **kwargs, config=RuntimeConfig()
    )
    app = create_a2a_application(service=service, authenticator=Authenticator())
    return TestClient(app)


def _send(client: TestClient, parts: list[dict[str, Any]], **extra: Any) -> Any:
    message = {"role": "user", "parts": parts, "messageId": "m-1", **extra}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "message/send",
        "params": {"message": message},
    }
    return client.post("/a2a", json=body, headers=HEADERS)


def _call(**body: Any) -> list[dict[str, Any]]:
    return [{"kind": "data", "data": {"toolTask": {"operation": "x.read", **body}}}]


@pytest.mark.spec("ARBORA-A2A-R002.2.4")
def test_card_lists_extension_only_when_composed_and_refuses_client_approval() -> None:
    card = "/.well-known/agent-card.json"
    bare = _client().get(card, headers=HEADERS).json()
    assert bare["capabilities"]["extensions"] == []
    half = _client(tool_task_admitter=Admitter()).get(card, headers=HEADERS).json()
    assert half["capabilities"]["extensions"] == []
    admitter = Admitter()
    client = _client(tool_task_admitter=admitter, attestation_signer=Signer())
    listed = client.get(card, headers=HEADERS).json()["capabilities"]["extensions"]
    assert [row["uri"] for row in listed] == [EXTENSION_URI]
    assert listed[0]["required"] is False

    ok = _send(client, _call(parameters={"limit": 2}))
    assert ok.status_code == 200
    assert admitter.calls == [("x.read", {"limit": 2}, "send-key")]
    meta = ok.json()["result"]["metadata"]["arbora"]
    assert meta["approvalRecordId"] == RECORD and meta["digest"] == DIGEST

    for refused in (
        _send(client, _call(preapprovalRef="approval:forged")),
        _send(client, _call(approval={"id": "x"})),
        _send(
            client,
            _call(),
            metadata={"arbora": {"approvalRecordId": RECORD}},
        ),
    ):
        assert refused.json()["error"]["code"] == -32602
    assert len(admitter.calls) == 1


@pytest.mark.spec("ARBORA-A2A-R002.2.4")
def test_unserved_tool_task_part_is_refused_and_creates_no_task() -> None:
    answer = _send(_client(), _call())
    assert answer.json()["error"]["code"] == -32003
    assert "tool_task_unavailable" in answer.json()["error"]["message"]


@pytest.mark.spec("ARBORA-A2A-R002.2.4")
def test_attestation_binds_task_record_and_digest() -> None:
    signer = Signer()
    public = signer.key.public_key()
    signed = sign_attestation(
        signer, task_id="t-1", approval_record_id=RECORD, digest=DIGEST
    )
    assert verify_attestation(public, signed, task_id="t-1")
    assert not verify_attestation(public, signed, task_id="t-2")
    for changed in (
        {"approval_record_id": "approval:other"},
        {"digest": "b" * 64},
    ):
        forged = ToolTaskAttestation(**{**signed.model_dump(), **changed})
        assert not verify_attestation(public, forged, task_id="t-1")
    assert not verify_attestation(
        Ed25519PrivateKey.generate().public_key(), signed, task_id="t-1"
    )
    assert set(signed.as_metadata()) == {
        "approvalRecordId",
        "digest",
        "signature",
        "keyId",
    }
