"""The shared signed inbound webhook ingress (ARBORA-MESSAGING-R008.1)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arbora.messaging.backends._channel_http import sign_body
from arbora.messaging.backends.sms import SmsBackend
from arbora.messaging.models import MessagingConfig
from arbora.messaging.service import MessagingService
from arbora.messaging.webhook_ingress import register_inbound_route

_BODY = json.dumps({"id": "1", "from": "+1555", "body": "hi"}).encode()


class _Service:
    def __init__(self, backend: SmsBackend) -> None:
        self._backend = backend

    async def get_backend(self, platform: str) -> SmsBackend | None:
        return self._backend if platform == "sms" else None


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, SmsBackend]:
    monkeypatch.setenv("MESSAGING_INTAKE_ENABLED", "true")
    monkeypatch.setenv("KG_DAEMON_ROLE", "host")
    monkeypatch.setattr(
        "arbora.messaging.backends._channel_http.resolve_secret", lambda ref: "s3cret"
    )
    backend = SmsBackend(MessagingConfig(platform="sms"))
    backend._connected = True
    monkeypatch.setattr(
        MessagingService,
        "instance",
        classmethod(lambda cls, engine=None: _Service(backend)),
    )
    app = Starlette()
    register_inbound_route(app)
    return TestClient(app), backend


def _post(client: TestClient, platform: str, body: bytes, signature: str) -> Any:
    return client.post(
        f"/api/v1/messaging/inbound/{platform}",
        content=body,
        headers={"X-Arbora-Signature": signature},
    )


@pytest.mark.spec("ARBORA-MESSAGING-R008.1")
def test_signed_event_reaches_backend_queue_and_bad_signature_refuses(
    served: tuple[TestClient, SmsBackend], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, backend = served
    ok = _post(client, "sms", _BODY, sign_body("s3cret", _BODY))
    assert ok.status_code == 202
    assert backend._event_queue.get_nowait().content == "hi"

    bad = _post(client, "sms", _BODY, "sha256=00")
    assert (bad.status_code, bad.json()["error"]) == (401, "INBOUND_SIGNATURE_INVALID")
    assert backend._event_queue.empty()

    unknown = _post(client, "nowhere", _BODY, sign_body("s3cret", _BODY))
    assert (unknown.status_code, unknown.json()["error"]) == (
        404,
        "INBOUND_PLATFORM_UNKNOWN",
    )

    monkeypatch.setenv("MESSAGING_INBOUND_MAX_BYTES", "1024")
    big = b"x" * 2048
    too_large = _post(client, "sms", big, sign_body("s3cret", big))
    assert (too_large.status_code, too_large.json()["error"]) == (
        413,
        "INBOUND_TOO_LARGE",
    )

    monkeypatch.setenv("MESSAGING_INTAKE_ENABLED", "false")
    off = _post(client, "sms", _BODY, sign_body("s3cret", _BODY))
    assert (off.status_code, off.json()["error"]) == (503, "INBOUND_INTAKE_DISABLED")


@pytest.mark.spec("ARBORA-MESSAGING-R008.1")
def test_refusal_log_names_platform_and_code_never_the_body(
    served: tuple[TestClient, SmsBackend], caplog: pytest.LogCaptureFixture
) -> None:
    client, _ = served
    _post(client, "sms", _BODY, "sha256=00")
    text = caplog.text
    assert "platform=sms" in text and "INBOUND_SIGNATURE_INVALID" in text
    assert "+1555" not in text
