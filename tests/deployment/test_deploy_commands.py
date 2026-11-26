"""Deployment behaviour is published commands driven by a declared file (ARBORA-DEPLOY-R602)."""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from arbora.deployment import deployed_source


def _git(directory: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(directory), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def _repo(root: Path) -> Path:
    source = root / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    _git(source, "config", "user.email", "t@example.test")
    _git(source, "config", "user.name", "t")
    (source / "f").write_text("1")
    _git(source, "add", "f")
    _git(source, "commit", "-q", "-m", "one")
    return source


@pytest.mark.spec("ARBORA-DEPLOY-R602")
def test_each_script_function_is_a_command_with_a_config_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployed-source manager takes its locations from a declared file."""
    monkeypatch.delenv(deployed_source.SOURCE_REPO_ENV, raising=False)
    monkeypatch.delenv(deployed_source.DEPLOYED_WORKTREE_ENV, raising=False)
    source = _repo(tmp_path)
    config = tmp_path / "deploy.yml"
    config.write_text(
        yaml.safe_dump(
            {
                "deployed_source": {
                    "source_repo": str(source),
                    "worktree": str(tmp_path / "deployed"),
                }
            }
        )
    )
    assert deployed_source.main(["--config", str(config), "status"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["worktree"] == str((tmp_path / "deployed").resolve())


@pytest.mark.spec("ARBORA-DEPLOY-R602")
def test_no_built_in_location_exists(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With nothing declared the command refuses instead of using a host default."""
    monkeypatch.delenv(deployed_source.SOURCE_REPO_ENV, raising=False)
    monkeypatch.delenv(deployed_source.DEPLOYED_WORKTREE_ENV, raising=False)
    assert deployed_source.main(["status"]) == 2
    assert "required" in capsys.readouterr().err
    text = Path(deployed_source.__file__).read_text(encoding="utf-8")
    assert "/home/" not in text and ".arpa" not in text


#: Every function the deployment repository's code held, with the command and the
#: package function that replaced it (ARBORA-DEPLOY-R602.6). Its removal is the
#: deployment repository's own step; this table is the package side of that cutover.
REMOVED_FUNCTIONS: dict[str, tuple[tuple[str, ...], str, str]] = {
    "render.py:validate": (
        ("deploy", "render"),
        "arbora.deployment.render",
        "validate",
    ),
    "render.py:_render": (("deploy", "render"), "arbora.deployment.render", "_render"),
    "render_job.py:first_start_job": (
        ("deploy", "render"),
        "arbora.deployment.render_job",
        "first_start_job",
    ),
    "make_test_manifests.py:main": (
        ("deploy", "render"),
        "arbora.deployment.render_job",
        "prerelease_manifests",
    ),
    "provision.py:main": (
        ("deploy", "provision"),
        "arbora.deployment.provision",
        "provision",
    ),
    "enrol_principals.py:main": (
        ("deploy", "enrol"),
        "arbora.deployment.provision",
        "enrol",
    ),
    "normalize_config.py:main": (
        ("deploy", "normalize-config"),
        "arbora.deployment.provision",
        "normalize_config",
    ),
    "sso_probe.py:main": (
        ("deploy", "probe-sso"),
        "arbora.deployment.provision",
        "probe_sso",
    ),
    "inpod_check.py:main": (
        ("deploy", "check-in-pod"),
        "arbora.deployment.provision",
        "check_in_pod",
    ),
    "secret_diagnostics.py:main": (
        ("secrets", "diagnose"),
        "arbora.security.secret_diagnostics",
        "main",
    ),
    "deploy.sh": (("deploy", "run"), "arbora.deployment.run", "DeployRun"),
    "deployed_source.py:main": (
        ("deploy", "source"),
        "arbora.deployment.deployed_source",
        "main",
    ),
    "acceptance.py:main": (
        ("acceptance", "run"),
        "arbora.deployment.acceptance.runner",
        "main",
    ),
    "acceptance.py:fleet_servers": (
        ("acceptance", "run"),
        "arbora.deployment.acceptance.fleet_audit",
        "fleet_servers",
    ),
    "surface_audit.py:main": (
        ("acceptance", "run"),
        "arbora.deployment.acceptance.fleet_audit",
        "sweep",
    ),
    "scenario script:main": (
        ("acceptance", "run"),
        "arbora.deployment.acceptance.scenarios",
        "run_scenario",
    ),
}
#: Command words dispatched to the module that owns them.
COMMAND_MODULES = {
    ("deploy", "render"): "arbora.deployment.cli",
    ("deploy", "run"): "arbora.deployment.run",
    ("deploy", "source"): "arbora.deployment.deployed_source",
    ("secrets", "diagnose"): "arbora.security.secret_diagnostics",
    ("acceptance", "run"): "arbora.deployment.acceptance.runner",
}


@pytest.mark.spec("ARBORA-DEPLOY-R602.6")
def test_each_script_function_is_a_command_with_a_config_input_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every removed function names a command that the console dispatches to a real target."""
    from arbora import console

    for removed, (command, module, attribute) in REMOVED_FUNCTIONS.items():
        target = importlib.import_module(module)
        assert hasattr(target, attribute), f"{removed} -> {module}.{attribute}"
        owner = COMMAND_MODULES.get(command, "arbora.deployment.provision")
        handler = importlib.import_module(owner)
        calls: list[list[str]] = []
        monkeypatch.setattr(
            handler,
            "main",
            lambda argv=None, _c=calls: _c.append(list(argv or [])) or 0,
        )
        monkeypatch.setattr("sys.argv", ["arbora", *command, "--config", "x.yml"])
        with pytest.raises(SystemExit) as stopped:
            console.main()
        assert stopped.value.code == 0, removed
        assert calls, f"{removed}: {command} did not reach {owner}"
