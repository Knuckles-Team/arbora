"""Deployment behaviour is published commands driven by a declared file (ARBORA-DEPLOY-R602)."""

from __future__ import annotations

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
