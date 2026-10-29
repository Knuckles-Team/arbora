"""The pull-request workflow follows the fleet fast-PR standard."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def _load(name: str) -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(
        (WORKFLOWS / name).read_text(encoding="utf-8")
    )
    return loaded


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    """PyYAML (YAML 1.1) reads the bare `on:` key as the boolean True."""
    return cast("dict[str, Any]", workflow.get("on", workflow.get(True)))


def _ci() -> dict[Any, Any]:
    return _load("ci.yml")


def test_ci_triggers_and_caller_side_concurrency() -> None:
    workflow = _ci()
    triggers = _triggers(workflow)
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
    # Heavy checks run on main and nightly through checks.yml, never on a PR.
    assert jobs["main-checks"]["if"] == "github.event_name != 'pull_request'"
    assert jobs["main-checks"]["uses"] == "./.github/workflows/checks.yml"
    for name in ("gates", "build"):
        text = yaml.safe_dump(jobs[name])
        assert "3.12" not in text
    assert jobs["gates"]["timeout-minutes"] <= 20
    assert jobs["build"]["timeout-minutes"] <= 10


def test_ci_never_writes_and_never_calls_the_release_workflow() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "release.yml" not in yaml.safe_dump(_ci())
    assert "uses: ./.github/workflows/release.yml" not in text

    def permission_values(node: Any) -> list[Any]:
        found: list[Any] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "permissions":
                    found.append(value)
                found.extend(permission_values(value))
        elif isinstance(node, list):
            for item in node:
                found.extend(permission_values(item))
        return found

    granted = permission_values(_ci())
    assert granted and all(
        isinstance(grant, dict) and set(grant.values()) <= {"read", "none"}
        for grant in granted
    ), granted
    assert "write" not in yaml.safe_dump(granted)
