"""``arbora clients enroll`` composes the realm admin from the console config.

ARBORA-IDENTITY-R030.1: landed means reachable, so the test enters through the console
composition root with a config path and reaches the Keycloak adapter and the secret store,
not through injected ports.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arbora import console
from arbora.deployment import provision


class _Vault:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def set_if_absent(self, key: str, value: str, **_: Any) -> bool:
        return self.data.setdefault(key, value) == value


def _admin(calls: list[tuple]) -> Any:
    def admin(path: str, method: str = "GET", body: object | None = None) -> Any:
        calls.append((path, method, body))
        return [] if method == "GET" else None

    return admin


@pytest.mark.spec("ARBORA-IDENTITY-R030.1")
def test_enroll_composes_the_keycloak_adapter_from_the_console_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    calls: list[tuple] = []
    vault = _Vault()
    config = tmp_path / "deploy.yml"
    config.write_text("placeholder: true\n")
    monkeypatch.setattr(provision.DeployConfig, "load", classmethod(lambda cls, p: p))
    monkeypatch.setattr(
        provision.Ports,
        "real",
        classmethod(lambda cls, c: SimpleNamespace(admin=_admin(calls))),
    )
    import arbora.security.secrets as secrets

    monkeypatch.setattr(secrets, "create_secrets_client", lambda *a, **k: vault)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "arbora",
            "clients",
            "enroll",
            "claude-code",
            "--scope",
            "kg:read",
            "--config",
            str(config),
        ],
    )
    with pytest.raises(SystemExit) as done:
        console.main()
    assert done.value.code == 0
    out = capsys.readouterr()
    assert out.out.strip() == "openbao://arbora/clients/claude-code/secret"
    assert [c[:2] for c in calls] == [
        ("/clients?clientId=claude-code", "GET"),
        ("/clients", "POST"),
    ]
    assert vault.data["arbora/clients/claude-code/secret"] not in out.out + out.err


@pytest.mark.spec("ARBORA-IDENTITY-R030.1")
def test_enroll_without_a_config_still_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ARBORA_DEPLOY_CONFIG", raising=False)
    monkeypatch.setattr(sys, "argv", ["arbora", "clients", "enroll", "claude-code"])
    with pytest.raises(SystemExit) as done:
        console.main()
    assert done.value.code == 2
    assert "PROVIDER_ADMIN_UNAVAILABLE" in capsys.readouterr().err
