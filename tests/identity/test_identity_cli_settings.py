"""The identity CLI, deployment names and probe script named by the runbook (ARBORA-IDENTITY-R015)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from arbora.identity import cli
from arbora.identity.deployment_settings import identity_deployment


@pytest.mark.spec("ARBORA-IDENTITY-R015")
def test_unserved_commands_refuse_unavailable_and_unknown_ones_print_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    for command in cli.PENDING_COMMANDS:
        assert cli.main([command]) == cli.UNAVAILABLE_EXIT
        assert json.loads(capsys.readouterr().out)["status"] == "UNAVAILABLE"
    assert cli.main(["nonsense"]) == cli.USAGE_EXIT
    assert cli.main([]) == cli.USAGE_EXIT


@pytest.mark.spec("ARBORA-IDENTITY-R015")
def test_deployment_names_are_read_afresh_and_the_setup_code_never_leaves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "ARBORA_IDENTITY_ISSUER",
        "ARBORA_IDENTITY_TENANT",
        "ARBORA_SETUP_CODE",
    ):
        monkeypatch.delenv(name, raising=False)
    assert identity_deployment().report() == {
        "ARBORA_IDENTITY_ISSUER": False,
        "ARBORA_IDENTITY_TENANT": False,
        "ARBORA_SETUP_CODE": False,
    }
    monkeypatch.setenv("ARBORA_IDENTITY_ISSUER", "https://issuer.test")
    monkeypatch.setenv("ARBORA_IDENTITY_TENANT", "t1")
    monkeypatch.setenv("ARBORA_SETUP_CODE", "s3cret-code")
    deployed = identity_deployment()
    assert (deployed.issuer, deployed.tenant) == ("https://issuer.test", "t1")
    assert "s3cret-code" not in repr(deployed) + json.dumps(deployed.report())
    assert deployed.report()["ARBORA_SETUP_CODE"] is True


@pytest.mark.spec("ARBORA-IDENTITY-R015")
def test_probe_output_holds_only_statuses_and_refuses_an_unreachable_deployment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = Path(__file__).resolve().parents[2] / "scripts" / "identity_probe.py"
    spec = importlib.util.spec_from_file_location("identity_probe", path)
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.main(["--base-url", "http://127.0.0.1:9"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and set(out["checks"].values()) == {False}
    assert probe.main(["--base-url", "http://x", "--protocol", "saml"]) == 2
