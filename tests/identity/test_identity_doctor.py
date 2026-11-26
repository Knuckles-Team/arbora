"""Doctor fails for an exposed none mode and a generated secret (ARBORA-IDENTITY-R013.3)."""

from __future__ import annotations

import pytest

from arbora.core.settings import runtime_config
from arbora.deployment import doctor


@pytest.mark.spec("ARBORA-IDENTITY-R013.3")
def test_doctor_fails_for_exposed_mode_and_generated_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ARBORA_AUTH_NONE_EXPOSE", raising=False)
    monkeypatch.setattr(runtime_config, "arbora_auth_mode", "required")
    monkeypatch.setattr(runtime_config, "host", "127.0.0.1")
    assert doctor._check_identity_exposure()["status"] == "ok"

    monkeypatch.setattr(runtime_config, "arbora_auth_mode", "none")
    monkeypatch.setattr(runtime_config, "host", "0.0.0.0")
    exposed = doctor._check_identity_exposure()
    assert exposed["status"] == "fail"
    assert "identity.exposed_none_mode" in exposed["data"]["checks"]

    monkeypatch.setattr(runtime_config, "host", "127.0.0.1")
    monkeypatch.setenv("ARBORA_AUTH_NONE_EXPOSE", "1")
    assert doctor._check_identity_exposure()["status"] == "fail"

    monkeypatch.delenv("ARBORA_AUTH_NONE_EXPOSE")
    monkeypatch.setattr(runtime_config, "arbora_auth_mode", "required")
    monkeypatch.setenv("ARBORA_PROFILE", "production")
    monkeypatch.setenv("GRAPH_SERVICE_AUTH_SECRET_GENERATED", "1")
    generated = doctor._check_identity_exposure()
    assert generated["status"] == "fail"
    assert generated["data"]["checks"] == ["identity.generated_secret"]
