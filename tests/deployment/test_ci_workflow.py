"""The pull-request workflow follows the fleet fast-PR standard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _ci() -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


def test_ci_triggers_and_caller_side_concurrency() -> None:
    workflow = _ci()
    triggers = workflow[True]
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert triggers["push"] == {"branches": ["main"]}
    assert "schedule" in triggers and "workflow_dispatch" in triggers
    assert workflow["concurrency"]["cancel-in-progress"] is True


def test_no_skip_companion_and_shared_auto_merge_caller() -> None:
    assert not list(WORKFLOWS.glob("*-skip.yml"))
    text = (WORKFLOWS / "auto_merge.yml").read_text(encoding="utf-8")
    assert (
        "Knuckles-Team/pipelines/.github/workflows/auto_merge_pipeline.yml@main" in text
    )


def test_ci_required_aggregates_every_blocking_job() -> None:
    jobs = _ci()["jobs"]
    required = jobs["ci-required"]
    assert required["name"] == "CI required"
    assert required["if"] == "always()"
    assert {"gates", "build", "main-checks"} <= set(required["needs"])
    # The ruleset's current check names keep reporting until it requires `CI required`.
    assert jobs["gates"]["name"] == "gates" and jobs["build"]["name"] == "build"


def test_pull_requests_run_only_fast_python_314_jobs() -> None:
    jobs = _ci()["jobs"]
    # Heavy checks run on main and nightly through release.yml, never on a PR.
    assert jobs["main-checks"]["if"] == "github.event_name != 'pull_request'"
    assert jobs["main-checks"]["uses"] == "./.github/workflows/release.yml"
    for name in ("gates", "build"):
        text = yaml.safe_dump(jobs[name])
        assert "3.12" not in text
    assert jobs["gates"]["timeout-minutes"] <= 20
    assert jobs["build"]["timeout-minutes"] <= 10
