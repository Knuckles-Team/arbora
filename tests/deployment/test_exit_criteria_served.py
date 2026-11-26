# ruff: noqa: F811
"""The release plan audits the exit-criteria matrix (ARBORA-RELEASE-R003.10)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from arbora.deployment.cli import main
from arbora.deployment.exit_criteria import passed_node_ids_from_junit
from arbora.deployment.release_candidate import default_matrix_path
from tests.deployment.test_release_candidate import (  # noqa: F401
    _write,
    authority,
    candidate,
)


def _junit(
    path: Path, skipped: set[str] = frozenset(), absent: set[str] = frozenset()
) -> Path:  # type: ignore[assignment]
    rows = json.loads(default_matrix_path().read_text())["rows"]
    cases = []
    for row in rows:
        if row["obligation_id"] in absent:
            continue
        file, *owner, name = row["test_reference"].split("::")
        classname = file.removesuffix(".py").replace("/", ".")
        mark = "<skipped/>" if row["obligation_id"] in skipped else ""
        cases.append(
            f'<testcase classname="{classname}" name="{name}">{mark}</testcase>'
        )
    path.write_text(f"<testsuites><testsuite>{''.join(cases)}</testsuite></testsuites>")
    return path


def _argv(plan: Path, digest: str, report: Path) -> list[str]:
    return [
        "release-plan", str(plan), "--trusted-digest", digest,
        "--profile", "prod", "--junit", str(report),
    ]  # fmt: skip


@pytest.mark.spec("ARBORA-RELEASE-R003.10")
def test_release_plan_refuses_unmet_obligation(
    tmp_path: Path,
    candidate: Any,
    authority: Any,
    capsys: pytest.CaptureFixture[str],  # noqa: F811
) -> None:
    plan, digest = _write(tmp_path, candidate)
    report = _junit(tmp_path / "report.xml", absent={"typed_abstention"})
    assert main(_argv(plan, digest, report)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "EXIT_CRITERIA_UNMET"
    assert out["missing"] == ["typed_abstention"]
    assert out["executed"] is False


@pytest.mark.spec("ARBORA-RELEASE-R003.10")
def test_release_plan_passes_when_every_obligation_passed(
    tmp_path: Path,
    candidate: Any,
    authority: Any,
    capsys: pytest.CaptureFixture[str],  # noqa: F811
) -> None:
    plan, digest = _write(tmp_path, candidate)
    assert main(_argv(plan, digest, _junit(tmp_path / "ok.xml"))) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["acceptance"] == "audited" and len(out["satisfied"]) == 7


@pytest.mark.spec("ARBORA-RELEASE-R003.10")
def test_skipped_test_is_not_a_pass(
    tmp_path: Path,
    candidate: Any,
    authority: Any,
    capsys: pytest.CaptureFixture[str],  # noqa: F811
) -> None:
    report = _junit(tmp_path / "skip.xml", skipped={"login_probe"})
    assert (
        "tests/release/test_exit_login_probe.py::test_browser_login_through_the_issuer_reaches_a_verified_session"
        not in passed_node_ids_from_junit(report)
    )
    plan, digest = _write(tmp_path, candidate)
    assert main(_argv(plan, digest, report)) == 1
    assert json.loads(capsys.readouterr().out)["missing"] == ["login_probe"]
