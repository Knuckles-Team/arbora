"""The pull-request fast subset always carries the static architecture tests.

The mapped subset narrows the suite to the tests a diff affects, so a rule that
scans the whole repository is only checked when its own test is mapped. These
tests pin that the release workflow adds the static set to every non-full scope.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from types import ModuleType

import pytest
import yaml

pytestmark = pytest.mark.spec("ARBORA-RELEASE-R002")

ROOT = Path(__file__).parents[2]
NAMED = (
    "tests/architecture/test_identity_ports_boundary.py",
    "tests/identity/test_identity_ports_boundary.py",
    "tests/identity/test_port_only_dependents.py",
)


def _scope_module() -> ModuleType:
    path = ROOT / "scripts" / "pr_static_scope.py"
    spec = importlib.util.spec_from_file_location("pr_static_scope", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fast_subset_run() -> str:
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["gates"]["steps"]
    return next(
        step["run"]
        for step in steps
        if "pull request fast subset" in step.get("name", "")
    )


def test_static_set_names_the_identity_boundary_tests() -> None:
    selected = _scope_module().static_scan_tests()
    for path in NAMED:
        assert path in selected


def test_every_architecture_test_is_selected() -> None:
    selected = set(_scope_module().static_scan_tests())
    on_disk = {
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests" / "architecture").rglob("test_*.py")
    }
    assert on_disk <= selected


def test_every_selected_test_exists() -> None:
    for path in _scope_module().static_scan_tests():
        assert (ROOT / path).is_file(), path


def test_every_source_scanning_test_is_selected() -> None:
    module = _scope_module()
    selected = set(module.static_scan_tests())
    scanners = {
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "tests").rglob("test_*.py")
        if p.name != Path(__file__).name
        and module.SCAN_MARKERS.search(p.read_text(encoding="utf-8"))
    }
    assert scanners <= selected, sorted(scanners - selected)


def test_workflow_adds_the_static_set_to_every_non_full_scope() -> None:
    run = _fast_subset_run()
    assert "scripts/pr_static_scope.py" in run
    full, _, mapped = run.partition('if [ "$SCOPE" = "FULL" ]')[2].partition("else")
    assert "scripts/local_hook.sh pytest\n" in full
    assert '"${static_tests[@]}"' in mapped
    # A diff with no mapped test must still run the static set, never skip pytest.
    assert "skipping pytest" not in run


@pytest.mark.parametrize("scope", ["NONE", "tests/mapped/test_user.py", "FULL"])
@pytest.mark.parametrize("producer_exit", [0, 7])
def test_static_scope_producer_failure_blocks_pytest(
    tmp_path: Path, scope: str, producer_exit: int
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pr_static_scope.py").write_text(
        'print("tests/static/test_one.py")\n'
        'print("tests/static/test_two.py")\n'
        f"raise SystemExit({producer_exit})\n",
        encoding="utf-8",
    )
    hook = scripts / "local_hook.sh"
    hook.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$CAPTURE"\n', encoding="utf-8")
    hook.chmod(0o700)
    capture = tmp_path / "pytest-arguments"
    result = subprocess.run(
        ["bash", "-c", _fast_subset_run()],
        cwd=tmp_path,
        env={
            "PATH": os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "SCOPE": scope,
            "CAPTURE": str(capture),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == producer_exit, result.stderr
    if producer_exit:
        assert not capture.exists()
        return
    expected = ["pytest"]
    if scope != "FULL":
        if scope != "NONE":
            expected.append(scope)
        expected.extend(["tests/static/test_one.py", "tests/static/test_two.py"])
    assert capture.read_text(encoding="utf-8").splitlines() == expected
