"""Config migration: retired-key stripping + doctor auto-migration (WS-A)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbora.core.settings import plaintext_secret_keys
from arbora.deployment.config_generator import (
    config_doctor,
    migrate_config_file,
)

_RETIRED_ENGINE_KEY = "ENGINE_MODE"
_RETIRED_MESSAGING_TRIGGER = "MESSAGING_VENDOR_TRIGGER"
_RETIRED_MESSAGING_MODEL = "MESSAGING_VENDOR_MODEL"


@pytest.fixture(autouse=True)
def governed_retired_configuration_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Path:
    config_dir = tmp_path / "operator-config"
    policy = config_dir / "governance" / "retired-configuration-keys.json"
    policy.parent.mkdir(parents=True)
    policy.write_text(
        json.dumps(
            {
                "version": "test-v1",
                "renames": {
                    _RETIRED_MESSAGING_TRIGGER: "MESSAGING_MODEL_TRIGGER",
                    _RETIRED_MESSAGING_MODEL: "MESSAGING_ADDRESSED_MODEL",
                    "MESSAGING_LOCAL_MODEL": "MESSAGING_DEFAULT_MODEL",
                },
            }
        )
    )
    monkeypatch.setenv("AGENT_UTILITIES_CONFIG_DIR", str(config_dir))
    return policy


def test_migrate_config_file_preserves_non_selector_model_keys(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    original = json.dumps({"MESSAGING_VOICE_MODEL": "speech-model"})
    p.write_text(original)

    report = migrate_config_file(p, backup=False)

    assert report["status"] == "ok"
    assert p.read_text() == original


def test_migrate_config_file_does_not_guess_unlisted_provider_keys(
    tmp_path: Path,
) -> None:
    p = tmp_path / "config.json"
    original = json.dumps({"MESSAGING_UNLISTED_MODEL": "model-v1"})
    p.write_text(original)

    report = migrate_config_file(p, backup=False)

    assert report["status"] == "ok"
    assert p.read_text() == original


def test_migrate_config_file_aggressive_strips_unknown(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"WORKSPACE_PATH": "a", "CAMUNDA_URL": "http://c"}))
    report = migrate_config_file(p, strip_unknown=True)
    assert report["status"] == "migrated"
    assert "CAMUNDA_URL" in report["unknown_removed"]
    assert "CAMUNDA_URL" not in json.loads(p.read_text())


def test_plaintext_secret_keys_flags_inline_credentials() -> None:
    offenders = plaintext_secret_keys(
        {
            "GITLAB_TOKEN": "ghp_plaintext",  # credential suffix, not a *_REF
            "LANGFUSE_SECRET_KEY": "sk-live",  # credential suffix
            "PASSWORD": "hunter2",  # sensitive mapping key
            "GITLAB_TOKEN_REF": "vault://apps/gitlab#token",  # a *_REF -> allowed
            "WORKSPACE_PATH": "/x",  # ordinary field
            "TIMEOUT_SECONDS": 30,  # ordinary field
        }
    )
    assert offenders == ["GITLAB_TOKEN", "LANGFUSE_SECRET_KEY", "PASSWORD"]


def test_plaintext_secret_keys_empty_when_all_refs() -> None:
    assert (
        plaintext_secret_keys(
            {
                "OIDC_CLIENT_SECRET_REF": "vault://apps/x#s",
                "WORKSPACE_PATH": "/x",
                "GITLAB_TOKEN": "",  # empty placeholder is not a live secret
            }
        )
        == []
    )


def test_config_doctor_flags_plaintext_secret(tmp_path: Path) -> None:
    """The doctor must NAME an inline plaintext secret (the load path would else
    reject the whole config with an opaque DurableSecretError)."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"GITLAB_TOKEN": "ghp_plaintext", "WORKSPACE_PATH": "a"}))
    report = config_doctor(profile="tiny", config_path=p)
    assert report["status"] == "needs_migration"
    assert report["healthy"] is False
    check = next(c for c in report["checks"] if c["check"] == "durable_secret_policy")
    assert check["keys"] == ["GITLAB_TOKEN"]
    assert "_REF" in check["remediation"]


def test_migrate_config_file_reports_plaintext_secrets(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text(json.dumps({"TWENTY_TOKEN": "tok", "WORKSPACE_PATH": "a"}))
    report = migrate_config_file(p)
    assert "TWENTY_TOKEN" in report["plaintext_secrets"]
    # reported, never stripped or moved — the value stays put for a human-gated move
    assert "TWENTY_TOKEN" in json.loads(p.read_text())
