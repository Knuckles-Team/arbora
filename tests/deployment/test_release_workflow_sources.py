"""Structural contracts of the release workflow, derived from their sources of truth.

The workflow's own pins (tool versions, gate tooling commits) are the single
place a revision is recorded; these tests check that each pin is immutable and
wired in the right order, never that it equals a copy kept here. Every runtime
dependency installs from PyPI, so the workflow checks out no sibling source.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parents[2]
SHA = re.compile(r"[0-9a-f]{40}")
PRE_COMMIT_RUN = 'pre-commit run --config "$PINNED_PRECOMMIT"'


def _workflow() -> dict[str, Any]:
    path = ROOT / ".github" / "workflows" / "release.yml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _gate_steps() -> list[dict[str, Any]]:
    return _workflow()["jobs"]["gates"]["steps"]


def _step_index(steps: list[dict[str, Any]], needle: str) -> int:
    return next(i for i, step in enumerate(steps) if needle in step.get("run", ""))


def _declared_paths() -> set[str]:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        value["path"]
        for value in pyproject["tool"]["uv"]["sources"].values()
        if "path" in value
    }


def _locked_paths() -> set[str]:
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    return {
        package["source"]["editable"]
        for package in locked["package"]
        if "editable" in package.get("source", {})
    }


def _manual_hook_ids() -> set[str]:
    config = yaml.safe_load(
        (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    return {
        hook["id"]
        for repo in config["repos"]
        for hook in repo["hooks"]
        if "manual" in hook.get("stages", [])
    }


def _manual_hooks_named(commands: str) -> set[str]:
    named = set(
        re.findall(
            re.escape(PRE_COMMIT_RUN) + r" ([a-z][a-z-]*) --hook-stage manual",
            commands,
        )
    )
    for group in re.findall(r"for hook in ([a-z -]+); do", commands):
        named |= set(group.split())
    return named


def test_release_builds_from_this_repository_and_pypi_only() -> None:
    """No sibling source is checked out; every locked package is an index install."""
    workflow = _workflow()
    product_checkouts = [
        (name, step["with"].get("repository"))
        for name, job in workflow["jobs"].items()
        for step in job["steps"]
        if step.get("uses", "").startswith("actions/checkout@")
        and step.get("with", {}).get("repository")
        not in (None, "Knuckles-Team/repository-manager")
    ]
    assert product_checkouts == []
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    for retired in (
        "agent-utilities",
        "agent-connector-sdk",
        "agent-webui",
        "epistemic-graph",
    ):
        assert f"repository: Knuckles-Team/{retired}" not in text
    assert _declared_paths() == set()
    # "." is this checkout itself; nothing else is an editable path install.
    assert _locked_paths() == {"."}


def test_every_uvx_runs_after_a_pinned_uv() -> None:
    for name, job in _workflow()["jobs"].items():
        steps = job["steps"]
        uvx = [i for i, step in enumerate(steps) if "uvx " in step.get("run", "")]
        if not uvx:
            continue
        setup = next(
            i
            for i, step in enumerate(steps)
            if "astral-sh/setup-uv@" in step.get("uses", "")
        )
        assert setup < min(uvx), name
        assert re.fullmatch(r"\d+\.\d+\.\d+", steps[setup]["with"]["version"]), name


def test_one_python_version_is_pinned_for_every_job() -> None:
    # actions/setup-python's downloaded CPython build needs AVX2 and crashes
    # on the AVX2-free homelab self-hosted runners `gates`/`build` may land
    # on, so the pin lives on the astral-sh/setup-uv step instead (its
    # python-build-standalone download has no such requirement); either
    # action's python-version counts here, so this still catches a second,
    # inconsistent pin however it is set.
    versions = {
        step["with"]["python-version"]
        for job in _workflow()["jobs"].values()
        for step in job["steps"]
        if "actions/setup-python@" in step.get("uses", "")
        or "astral-sh/setup-uv@" in step.get("uses", "")
        if "python-version" in step.get("with", {})
    }

    # scripts/bootstrap.sh reads the same pin from .python-version.
    assert len(versions) == 1
    pin = (
        (Path(__file__).parents[2] / ".python-version")
        .read_text(encoding="utf-8")
        .strip()
    )
    assert versions.pop() == pin == "3.14"


def test_gates_run_the_repository_pre_commit_configuration() -> None:
    steps = _gate_steps()
    commands = "\n".join(step.get("run", "") for step in steps)

    # Pull requests are diff-scoped, main and tags run every file.
    assert f"{PRE_COMMIT_RUN} $range --show-diff-on-failure" in commands
    assert f"{PRE_COMMIT_RUN} $range --hook-stage pre-push" in commands
    assert "--from-ref $PRE_COMMIT_FROM_REF --to-ref HEAD" in commands
    assert 'range="--all-files"' in commands
    assert {"pytest", "mypy-env"} <= _manual_hooks_named(commands)
    # The full suite runs only after the environment it needs is synced.
    assert _step_index(steps, "uv sync --frozen") < _step_index(steps, "pytest")


def test_every_manual_hook_ci_names_is_defined() -> None:
    commands = "\n".join(
        step.get("run", "")
        for job in _workflow()["jobs"].values()
        for step in job["steps"]
    )

    assert _manual_hooks_named(commands) <= _manual_hook_ids()


def test_engine_installs_from_the_locked_wheel_not_a_source_overlay() -> None:
    steps = _gate_steps()
    sync = steps[_step_index(steps, "uv sync --frozen")]["run"]
    verify = steps[_step_index(steps, "epistemic_graph.numeric")]["run"]
    commands = "\n".join(step.get("run", "") for step in steps)

    assert "--no-install-package" not in sync
    assert "PYTHONPATH" not in commands
    assert "build_numeric_kernel" not in commands
    assert not any("Swatinem/rust-cache@" in step.get("uses", "") for step in steps)
    assert 'read_text("direct_url.json")' in verify
    assert _step_index(steps, "uv sync --frozen") < steps.index(
        steps[_step_index(steps, "epistemic_graph.numeric")]
    )


def test_release_dependency_readiness_reports_on_tag_builds_only() -> None:
    workflow = _workflow()
    steps = workflow["jobs"]["gates"]["steps"]
    checkout = next(
        step
        for step in steps
        if step.get("name") == "Checkout pinned release-readiness implementation"
    )
    readiness = next(
        step
        for step in steps
        if step.get("name") == "Release dependency readiness (advisory, report-only)"
    )

    assert checkout["with"]["repository"] == "Knuckles-Team/repository-manager"
    assert SHA.fullmatch(checkout["with"]["ref"])
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["if"] == readiness["if"] == "startsWith(github.ref, 'refs/tags/v')"
    assert steps.index(checkout) < steps.index(readiness)
    # Advisory (operator gate cut): it reports and never blocks the build.
    assert checkout["continue-on-error"] is True
    assert readiness["continue-on-error"] is True
    assert "dependency-readiness --hook-stage manual" in readiness["run"]
    assert "ln -s" not in readiness["run"]
    hook = (ROOT / "scripts" / "local_hook.sh").read_text(encoding="utf-8")
    assert "uv run --no-project --no-sources" in hook
    assert workflow["jobs"]["build"]["needs"] == ["gates"]


def test_scanner_versions_receive_distinct_argv_and_the_job_is_advisory() -> None:
    scanner = _workflow()["jobs"]["scanner-quality"]
    command = next(
        step["run"]
        for step in scanner["steps"]
        if "pipelines-hook scanner-versions" in step.get("run", "")
    )

    assert "pipelines-hook scanner-versions cccc kiss dupehound jscpd" in command
    assert "['cccc'" not in command
    assert scanner["continue-on-error"] is True
    for job in ("build", "install-smoke", "publish-pypi", "github-release"):
        assert "scanner-quality" not in _workflow()["jobs"][job].get("needs", [])


def _scanner_step(name: str) -> dict[str, Any]:
    return next(
        step
        for step in _workflow()["jobs"]["scanner-quality"]["steps"]
        if step.get("name") == name
    )


def test_scanner_cache_retains_installation_and_all_checks_before_save() -> None:
    scanner = _workflow()["jobs"]["scanner-quality"]
    steps = scanner["steps"]
    restore = _scanner_step("Restore scanner toolchain")
    provision = _scanner_step("Provision pinned scanner toolchain")
    checks = _scanner_step(
        "Shared scanner hooks (versions, censuses, clone differentials)"
    )
    save = _scanner_step("Save verified scanner toolchain")

    assert steps.index(restore) < steps.index(provision) < steps.index(checks)
    assert steps.index(checks) < steps.index(save)
    # Both cold misses and warm hits take the same installer/verification path.
    assert "if" not in provision
    assert "if" not in checks
    assert provision["run"].startswith("bash scripts/install_scanners.sh ")
    assert "$RUNNER_TEMP/arbora-scanners" in provision["run"]
    assert set(_manual_hooks_named(checks["run"])) == {
        "complexity-census",
        "kiss-census",
        "dupehound-changed",
        "jscpd-differential",
        "jscpd-census",
    }
    assert save["if"] == "success() && steps.scanner-cache.outputs.cache-hit != 'true'"
    for step in (restore, provision, checks, save):
        assert "continue-on-error" not in step
    assert _workflow()["permissions"] == {"contents": "read"}
    assert "permissions" not in scanner


def test_scanner_cache_is_exact_and_contains_only_the_scanner_prefix() -> None:
    restore = _scanner_step("Restore scanner toolchain")
    save = _scanner_step("Save verified scanner toolchain")
    identity = _scanner_step("Resolve scanner cache identity")

    assert (
        restore["with"]
        == save["with"]
        == {
            "path": "${{ runner.temp }}/arbora-scanners",
            "key": "${{ steps.scanner-cache-identity.outputs.key }}",
        }
    )
    for operation, step in (("restore", restore), ("save", save)):
        action, _, revision = step["uses"].partition("@")
        assert action == f"actions/cache/{operation}"
        assert SHA.fullmatch(revision)
    assert identity["env"]["SCANNER_RECIPE"] == (
        "${{ hashFiles('scripts/install_scanners.sh') }}"
    )


@pytest.fixture
def scanner_identity_environment(tmp_path: Path) -> dict[str, str]:
    commands = tmp_path / "commands"
    commands.mkdir()
    for name in ("rustc", "cargo"):
        command = commands / name
        command.write_text(
            '#!/bin/sh\n[ "$FAIL_TOOL" != "' + name + '" ] || exit 23\n'
            'printf "%s\\n" "$TEST_' + name.upper() + '_IDENTITY"\n'
        )
        command.chmod(0o755)
    return {
        **os.environ,
        **_workflow()["jobs"]["scanner-quality"]["env"],
        "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "RUNNER_OS": "Linux",
        "RUNNER_ARCH": "X64",
        "ImageOS": "ubuntu24",
        "ImageVersion": "20261001.1.0",
        "SCANNER_RECIPE": "installer-and-provider-inputs",
        "TEST_RUSTC_IDENTITY": "rustc fixture compiler",
        "TEST_CARGO_IDENTITY": "cargo fixture compiler",
        "FAIL_TOOL": "",
        "RUSTFLAGS": "",
        "CARGO_ENCODED_RUSTFLAGS": "",
    }


def _scanner_cache_identity(env: dict[str, str]) -> tuple[int, str]:
    output = Path(env["GITHUB_OUTPUT"])
    output.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", "-c", _scanner_step("Resolve scanner cache identity")["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return result.returncode, output.read_text() if output.exists() else ""


@pytest.mark.parametrize(
    "changed_input",
    [
        "RUNNER_OS",
        "RUNNER_ARCH",
        "ImageOS",
        "ImageVersion",
        "SCANNER_RECIPE",
        "TEST_RUSTC_IDENTITY",
        "TEST_CARGO_IDENTITY",
        "CARGO_BUILD_JOBS",
        "CARGO_PROFILE_RELEASE_DEBUG",
        "RUSTFLAGS",
        "CARGO_ENCODED_RUSTFLAGS",
    ],
)
def test_scanner_cache_identity_changes_with_build_inputs(
    scanner_identity_environment: dict[str, str], changed_input: str
) -> None:
    env = scanner_identity_environment
    code, before = _scanner_cache_identity(env)
    assert code == 0
    assert re.fullmatch(r"key=arbora-scanners-v1-[0-9a-f]{64}\n", before)
    assert _scanner_cache_identity(env) == (0, before)
    code, after = _scanner_cache_identity({**env, changed_input: "changed"})
    assert code == 0
    assert after != before


@pytest.mark.parametrize("tool", ["rustc", "cargo"])
def test_scanner_cache_identity_fails_closed_without_a_compiler(
    scanner_identity_environment: dict[str, str], tool: str
) -> None:
    assert _scanner_cache_identity(
        {**scanner_identity_environment, "FAIL_TOOL": tool}
    ) == (23, "")


def test_only_superseded_pull_request_runs_are_cancelled() -> None:
    concurrency = _workflow()["concurrency"]
    group = concurrency["group"]
    assert "format('pr-{0}-{1}'" in group and "pull_request.number" in group
    assert "format('main-{0}'" in group and "format('release-{0}-{1}'" in group
    # Superseded PR and main runs cancel; only tag publishes are preserved.
    assert concurrency["cancel-in-progress"] == "${{ github.ref_type != 'tag' }}"


_SMOKE_PLATFORMS = {
    "linux-x86_64",
    "linux-aarch64",
    "macos-aarch64",
    "macos-x86_64",
    "windows-x86_64",
    "windows-aarch64",
}
# The platforms the epistemic-graph engine publishes wheels for gate the
# release; the other legs run and report but cannot pass until it does.
_REQUIRED_SMOKE_PLATFORMS = {
    "linux-x86_64",
    "linux-aarch64",
    "macos-aarch64",
    "windows-x86_64",
}


def _locked_wheel_platforms(package: str) -> set[str]:
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    entry = next(item for item in locked["package"] if item["name"] == package)
    tags = {wheel["url"].rsplit("-", 1)[-1] for wheel in entry["wheels"]}
    platforms = set()
    for tag in tags:
        if "manylinux" in tag:
            platforms.add("linux-x86_64" if "x86_64" in tag else "linux-aarch64")
        elif "macosx" in tag:
            platforms.add("macos-aarch64" if "arm64" in tag else "macos-x86_64")
        elif "win" in tag:
            platforms.add("windows-x86_64" if "amd64" in tag else "windows-aarch64")
    return platforms


def test_install_smoke_covers_every_os_and_architecture_before_publish() -> None:
    jobs = _workflow()["jobs"]
    smoke = jobs["install-smoke"]
    legs = smoke["strategy"]["matrix"]["include"]

    assert {leg["name"] for leg in legs} == _SMOKE_PLATFORMS
    required = {leg["name"] for leg in legs if not leg.get("optional")}
    assert required == _REQUIRED_SMOKE_PLATFORMS
    # A leg is optional only while the engine publishes no wheel for it.
    for package in ("epistemic-graph", "epistemic-graph-server"):
        assert _locked_wheel_platforms(package) == _REQUIRED_SMOKE_PLATFORMS
    assert smoke["strategy"]["fail-fast"] is False
    assert smoke["needs"] == ["build"]
    assert jobs["publish-pypi"]["needs"] == ["build", "install-smoke"]

    commands = "\n".join(step.get("run", "") for step in smoke["steps"])
    assert "uv export --frozen" in commands
    assert "--require-hashes --no-deps" in commands
    assert "--index-url https://pypi.org/simple" in commands
    assert "--no-deps dist/arbora-*.whl" in commands
    assert 'read_text("direct_url.json") is None' in commands


def test_github_release_links_the_published_pypi_version() -> None:
    release = _workflow()["jobs"]["github-release"]
    assert release["needs"] == ["publish-pypi"]
    notes = next(
        step for step in release["steps"] if step["name"] == "Write release notes"
    )
    assert "https://pypi.org/project/arbora/$version/" in notes["run"]
    assert 'pip install \\"arbora[webui]==$version\\"' in notes["run"]


def test_release_eg_resolves_from_the_package_index_as_wheels() -> None:
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    eg = next(
        package for package in locked["package"] if package["name"] == "epistemic-graph"
    )
    assert eg["source"] == {"registry": "https://pypi.org/simple"}
    assert eg.get("wheels")
    # EG 2.27.3 also publishes an sdist; the lock records it, but uv never
    # builds the engine from source.
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    no_build = set(project["tool"]["uv"].get("no-build-package", ()))
    assert {"epistemic-graph", "epistemic-graph-server"} <= no_build


def test_installed_sdk_contains_schema_disclosure_contract() -> None:
    """The SDK installs from the package index; read the installed module."""
    import ast
    import importlib.util

    spec = importlib.util.find_spec("agent_connector_sdk.mcp.schema_disclosure")
    assert spec is not None and spec.origin is not None
    source = Path(spec.origin).read_text(encoding="utf-8")
    names = {
        node.name
        for node in ast.parse(source).body
        if isinstance(node, (ast.FunctionDef, ast.ClassDef))
    }
    names |= {
        alias.asname or alias.name
        for node in ast.parse(source).body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert {"SchemaManifest", "reconstruct_schema"} <= names


@pytest.mark.parametrize(
    ("path", "required"),
    [
        ("scripts/gen_api.py", "true"),
        (".github/workflows/release.yml", "true"),
        ("scripts/install_scanners.sh", "true"),
        ("arbora/client/invoke.py", "true"),
        ("arbora/api/generated/registry.json", "true"),
        ("docs/api/openapi.json", "true"),
        ("arbora/a2a/application.py", "false"),
    ],
)
def test_scanner_scope_executes_only_for_generated_contract_changes(
    tmp_path, path, required
):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    source = tmp_path / path
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("before\n")
    subprocess.run(["git", "add", path], cwd=tmp_path, check=True)
    commit = [
        "git",
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.test",
        "commit",
        "-qm",
    ]
    subprocess.run([*commit, "base"], cwd=tmp_path, check=True)
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    source.write_text("after\n")
    subprocess.run(["git", "add", path], cwd=tmp_path, check=True)
    subprocess.run([*commit, "change"], cwd=tmp_path, check=True)
    output = tmp_path / "scope-output"
    script = _workflow()["jobs"]["scanner-scope"]["steps"][1]["run"]
    subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "BASE_SHA": base, "GITHUB_OUTPUT": str(output)},
        check=True,
    )
    assert output.read_text() == f"required={required}\n"


def test_generator_scanner_runs_for_main_and_selected_pr_without_publication():
    jobs = _workflow()["jobs"]
    condition = jobs["scanner-quality"]["if"]
    assert "github.event_name == 'push' && github.ref_type == 'branch'" in condition
    assert (
        "github.event_name == 'pull_request' && needs.scanner-scope.outputs.required == 'true'"
        in condition
    )
    assert "needs.freeze-gate-source.result == 'success'" in condition
    assert "scanner-scope" in jobs["scanner-quality"]["needs"]
    assert "if" not in jobs["build"]
    assert jobs["build"]["needs"] == ["gates"]
    build_commands = "\n".join(step.get("run", "") for step in jobs["build"]["steps"])
    assert "uv build --wheel --out-dir dist" in build_commands
    assert "wheel_boot_check.py contents" in build_commands
    assert jobs["install-smoke"]["if"] == "github.event_name != 'pull_request'"
    assert (
        jobs["publish-pypi"]["if"]
        == "github.ref_type == 'tag' && startsWith(github.ref_name, 'v')"
    )
