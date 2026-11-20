"""Regression tests for the shared-pipelines reference lint."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_wiring.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_SCRIPT), "reference-pins", "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.spec("ARBORA-DEPLOY-R012")
def test_pinned_commit_fails_and_main_passes(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "- repo: https://github.com/Knuckles-Team/pipelines\n  rev: main\n",
        encoding="utf-8",
    )
    (tmp_path / ".github" / "workflows" / "checks.yml").write_text(
        "jobs:\n  gate:\n    uses: Knuckles-Team/pipelines/.github/workflows/gates.yml@main\n",
        encoding="utf-8",
    )

    assert _run(tmp_path).returncode == 0

    (tmp_path / ".pre-commit-config.yaml").write_text(
        "- repo: https://github.com/Knuckles-Team/pipelines\n"
        "  rev: 0123456789abcdef0123456789abcdef01234567\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)

    assert result.returncode == 1
    assert "PIPELINES_REF_PINNED .pre-commit-config.yaml:2" in result.stdout


@pytest.mark.spec("ARBORA-DEPLOY-R012")
def test_workflow_tag_reference_fails(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "checks.yaml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "jobs:\n  gate:\n    uses: Knuckles-Team/pipelines/.github/workflows/gates.yml@v1\n",
        encoding="utf-8",
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert "PIPELINES_REF_PINNED .github/workflows/checks.yaml:3 (v1)" in result.stdout


@pytest.mark.spec("ARBORA-DEPLOY-R012")
def test_pre_commit_pipelines_entry_requires_a_reference(tmp_path: Path) -> None:
    (tmp_path / ".pre-commit-config.yaml").write_text(
        "- repo: https://github.com/Knuckles-Team/pipelines\n", encoding="utf-8"
    )

    result = _run(tmp_path)

    assert result.returncode == 1
    assert (
        "PIPELINES_REF_PINNED .pre-commit-config.yaml:1 (missing rev)" in result.stdout
    )
