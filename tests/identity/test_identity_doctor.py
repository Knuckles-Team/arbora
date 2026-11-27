"""Doctor fails for an exposed none mode and a generated secret (ARBORA-IDENTITY-R013.3)."""

from __future__ import annotations

import pytest

from arbora.core.settings import RuntimeConfig
from arbora.deployment import doctor


@pytest.mark.spec("ARBORA-IDENTITY-R013.3")
def test_doctor_fails_for_exposed_mode_and_generated_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = RuntimeConfig()
    monkeypatch.delenv("ARBORA_AUTH_NONE_EXPOSE", raising=False)
    config.arbora_auth_mode = "required"
    config.host = "127.0.0.1"
    assert doctor._check_identity_exposure(config)["status"] == "ok"

    config.arbora_auth_mode = "none"
    config.host = "0.0.0.0"
    exposed = doctor._check_identity_exposure(config)
    assert exposed["status"] == "fail"
    assert "identity.exposed_none_mode" in exposed["data"]["checks"]

    config.host = "127.0.0.1"
    monkeypatch.setenv("ARBORA_AUTH_NONE_EXPOSE", "1")
    assert doctor._check_identity_exposure(config)["status"] == "fail"

    monkeypatch.delenv("ARBORA_AUTH_NONE_EXPOSE")
    config.arbora_auth_mode = "required"
    monkeypatch.setenv("ARBORA_PROFILE", "production")
    monkeypatch.setenv("GRAPH_SERVICE_AUTH_SECRET_GENERATED", "1")
    generated = doctor._check_identity_exposure(config)
    assert generated["status"] == "fail"
    assert generated["data"]["checks"] == ["identity.generated_secret"]
