"""The arbora package units follow the layer order of the coupling policy.

The order is ``[[tool.pipelines_hooks.coupling.layers]]`` in ``pyproject.toml``,
the policy the pipelines ``coupling`` hook reads (PIPE-GATES-R088). The layer
tests run that hook, so the repository has one scanner and it is the shared one.

ARBORA-HOST-R038: a unit (a top-level module or package under ``arbora/``)
imports only its own layer or an earlier one, the unit import graph has no
cycle, and no unit reaches a leading-underscore module or name of another
unit. Function-local imports count: they are coupling a cold import hides.
"""

from __future__ import annotations

import ast
import functools
import shutil
import subprocess
import tomllib
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.spec("ARBORA-HOST-R038")

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "arbora"
HOOK_SOURCE = "git+https://github.com/Knuckles-Team/pipelines@main"


def load_layers(pyproject: Path) -> list[tuple[str, tuple[str, ...]]]:
    """The ``{name, units}`` tables the coupling hook reads, most stable first."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    layers = data["tool"]["pipelines_hooks"]["coupling"]["layers"]
    return [(layer["name"], tuple(layer["units"])) for layer in layers]


def package_units(package: Path) -> set[str]:
    units = set()
    for child in package.iterdir():
        if child.name == "__pycache__":
            continue
        if child.is_dir() and any(child.rglob("*.py")):
            units.add(child.name)
        elif child.suffix == ".py":
            units.add(child.stem)
    return units


def _module_name(package: Path, path: Path) -> tuple[list[str], bool]:
    parts = [package.name, *path.relative_to(package).with_suffix("").parts]
    if parts[-1] == "__init__":
        return parts[:-1], True
    return parts, False


def _imports(package: Path, path: Path) -> Iterator[tuple[int, list[str], str | None]]:
    """Yield ``(line, dotted target, imported name)`` for every arbora import."""
    module, is_package = _module_name(package, path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name.split("."), None
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = module if is_package else module[:-1]
                base = base[: len(base) - (node.level - 1)]
                target = [*base, *(node.module.split(".") if node.module else [])]
            else:
                target = (node.module or "").split(".")
            for alias in node.names:
                yield node.lineno, target, alias.name


@functools.cache
def coupling_run() -> tuple[int, list[str]]:
    """Run the shared ``coupling`` hook on this repository: exit code and finding lines."""
    installed = shutil.which("pipelines-hook")
    command = (
        [installed] if installed else ["uvx", "--from", HOOK_SOURCE, "pipelines-hook"]
    )
    done = subprocess.run(  # noqa: S603
        [*command, "coupling", "--root", str(ROOT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    lines = (done.stdout + done.stderr).splitlines()
    return done.returncode, [line for line in lines if line.strip()]


def finding_lines(*needles: str) -> list[str]:
    """The lines of the coupling hook's output that mention any of ``needles``."""
    _code, lines = coupling_run()
    return [line for line in lines if any(needle in line for needle in needles)]


def test_every_unit_is_named_in_exactly_one_layer() -> None:
    """The hook refuses a unit named twice or in no layer; no layer names a missing unit."""
    assert finding_lines("named in no layer", "twice") == []
    named = [u for _name, units in load_layers(ROOT / "pyproject.toml") for u in units]
    assert len(named) == len(set(named))
    assert sorted(set(named) - package_units(PACKAGE)) == []


def test_the_coupling_hook_is_enabled_in_the_gate_config() -> None:
    """The ``coupling`` hook of the shared pipelines repository runs at commit and push."""
    document = yaml.safe_load(
        (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    )
    hooks = {
        hook["id"]: hook
        for repo in document["repos"]
        if repo["repo"].endswith("Knuckles-Team/pipelines")
        for hook in repo["hooks"]
    }
    assert "pre-push" in hooks["coupling"]["stages"]


@pytest.mark.spec("ARBORA-HOST-R038.12")
def test_unit_imports_follow_layers_and_no_cycle_remains() -> None:
    """The whole policy at once: the hook exits 0 with no finding, with no expected failure."""
    code, lines = coupling_run()
    assert lines == []
    assert code == 0


HOST_UNITS = {
    "client",
    "console",
    "host_bindings",
    "local_browser_boundary",
    "local_browser_channel",
    "webui_host",
}
MOVED_COMPOSITION = {"registration", "serving", "browser_app", "browser_composition"}


def _units_imported(unit: str) -> set[str]:
    """Every dotted ``arbora`` target the files of ``unit`` import, function-local too."""
    imported: set[str] = set()
    for path in sorted((PACKAGE / unit).rglob("*.py")):
        for _line, target, name in _imports(PACKAGE, path):
            if target[0] == "arbora" and len(target) > 1:
                imported.add(".".join(target[1:3]))
                if name and len(target) == 2:
                    imported.add(name)
    return imported


@pytest.mark.spec("ARBORA-HOST-R038.5")
def test_api_and_identity_import_no_subsystem_or_host() -> None:
    """``api`` and ``identity`` import no host unit and none of the moved composition modules."""
    offenders = []
    for unit in ("api", "identity"):
        for target in sorted(_units_imported(unit)):
            top, _, second = target.partition(".")
            if top in HOST_UNITS or (
                top == "mcp_server" and second in MOVED_COMPOSITION
            ):
                offenders.append(f"{unit} -> {target}")
    assert offenders == []


@pytest.mark.spec("ARBORA-HOST-R038.1")
def test_api_ops_import_no_subsystem() -> None:
    """No file of ``api`` imports a unit of a later layer: handlers read ports instead."""
    upward = [
        line
        for line in finding_lines("a less stable layer")
        if line.startswith("arbora/api/")
    ]
    assert upward == []


@pytest.mark.spec("ARBORA-HOST-R038.8")
def test_no_unit_imports_another_units_private_name() -> None:
    """No unit imports a leading-underscore module or name of another unit."""
    assert finding_lines("import of private module") == []


@pytest.mark.spec("ARBORA-HOST-R038.7")
def test_no_cycle_remains_between_units_of_one_layer() -> None:
    """Units of one layer import each other in one direction only."""
    assert finding_lines("import cycle among units") == []


def _unit_pairs() -> Iterator[tuple[str, str]]:
    """Every ``(importing unit, imported unit)`` pair of the package, function-local too."""
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        source = path.relative_to(PACKAGE).parts[0].removesuffix(".py")
        for _line, target, name in _imports(PACKAGE, path):
            if target[0] == "arbora":
                yield source, (name if len(target) == 1 else target[1]) or ""


def _imported_by(*sources: str) -> set[tuple[str, str]]:
    """The ``(importing unit, imported unit)`` pairs whose importer is one of ``sources``."""
    return {(s, u) for s, u in _unit_pairs() if s in sources and s != u}


def _units_of(layer: str) -> set[str]:
    return set(dict(load_layers(ROOT / "pyproject.toml"))[layer])


@pytest.mark.spec("ARBORA-HOST-R038.2")
def test_fleet_gateway_deployment_do_not_import_mcp_server() -> None:
    """The fleet, the gateway and deployment receive host objects as arguments."""
    reached = _imported_by("fleet", "gateway", "deployment")
    assert sorted(pair for pair in reached if pair[1] == "mcp_server") == []


@pytest.mark.spec("ARBORA-HOST-R038.3")
def test_a2a_browser_modality_do_not_import_mcp_server() -> None:
    """A2A, browser control and modality receive the run state and the binding."""
    reached = _imported_by("a2a", "browser_control", "modality")
    assert sorted(pair for pair in reached if pair[1] == "mcp_server") == []


@pytest.mark.spec("ARBORA-HOST-R038.4")
def test_runs_plans_work_items_and_services_import_no_host_or_subsystem() -> None:
    """The run, plan and work item units and the three service edges stay downward."""
    later = _units_of("subsystems") | _units_of("host")
    reached = _imported_by(
        "agent_runs", "current_plan_publication", "work_items", "native_run_admission"
    )
    assert sorted(pair for pair in reached if pair[1] in later) == []
    assert ("observability", "deployment") not in _imported_by("observability")
    assert ("messaging", "work_items") not in _imported_by("messaging")
    service_edges = _imported_by("access")
    assert ("access", "agent_host") not in service_edges
    assert ("access", "identity") not in service_edges
