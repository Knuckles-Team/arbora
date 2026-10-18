from types import SimpleNamespace

import pytest

from arbora.messaging import alert_intake
from arbora.messaging.alert_intake import (
    _MAX_ALERT_CHARS,
    _handle,
    _loopback_bind,
    extract_alert_text,
)


def test_alert_intake_bind_and_payload_bounds():
    assert _loopback_bind("127.0.0.1")
    assert _loopback_bind("::1")
    assert _loopback_bind("localhost")
    assert not _loopback_bind("0.0.0.0")
    assert not _loopback_bind("example-service.internal")

    assert len(extract_alert_text("x" * (_MAX_ALERT_CHARS + 100))) == _MAX_ALERT_CHARS
    assert extract_alert_text({"alerts": [None, {"labels": "invalid"}]})


def test_alert_intake_token_resolution_fails_closed_without_reference(monkeypatch):
    monkeypatch.setattr(alert_intake, "setting", lambda *_args: "")
    assert alert_intake._resolve_alert_intake_token() is None


def test_alert_intake_token_resolution_uses_runtime_secret_reference(monkeypatch):
    monkeypatch.setattr(
        alert_intake,
        "setting",
        lambda key, *_args: (
            "env://ALERT_INTAKE_TOKEN"
            if key == "MESSAGING_ALERT_INTAKE_TOKEN_REF"
            else ""
        ),
    )

    monkeypatch.setenv("ALERT_INTAKE_TOKEN", "resolved-token")
    assert alert_intake._resolve_alert_intake_token() == "resolved-token"


@pytest.mark.asyncio
async def test_alert_intake_rejects_missing_or_wrong_bearer_before_processing():
    app = {"alert_intake_token": "expected"}
    missing = SimpleNamespace(headers={}, app=app)
    response = await _handle(missing)
    assert response.status == 401

    wrong = SimpleNamespace(headers={"Authorization": "Bearer wrong"}, app=app)
    response = await _handle(wrong)
    assert response.status == 401
