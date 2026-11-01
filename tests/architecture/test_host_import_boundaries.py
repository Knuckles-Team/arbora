"""Import-census tests for the host composition boundary.

ARBORA-HOST-R013: the web UI host package imports the agent runtime only
through ``agent_utilities.api``. It may also import Arbora itself and the graph
engine's client library.

ARBORA-HOST-R025: no Arbora module imports the agent runtime's retired MCP
serving stack (``intent_tools``, ``arbora_surface`` and ``kg_server``).
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"
WEBUI_ROOT = ARBORA_ROOT / "webui_host"

RETIRED_RUNTIME_MODULES = (
    "agent_utilities.mcp.tools.intent_tools",
    "agent_utilities.mcp.arbora_surface",
    "agent_utilities.mcp.kg_server",
    "agent_utilities.kg_server",
)


def _imported_modules(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module
            for alias in node.names:
                yield f"{node.module}.{alias.name}"
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value


def _is_allowed_runtime_import(module: str) -> bool:
    return module == "agent_utilities.api" or module.startswith("agent_utilities.api.")


def _webui_runtime_violations(root: Path) -> list[str]:
    violations = []
    for path in sorted(root.rglob("*.py")):
        for module in _imported_modules(path):
            top = module.split(".", 1)[0]
            if top == "meristem" and not _is_allowed_runtime_import(module):
                violations.append(f"{path.relative_to(root.parent)}: {module}")
    return sorted(set(violations))


@pytest.mark.spec("ARBORA-HOST-R013")
def test_webui_host_imports_only_public_runtime_surfaces() -> None:
    assert WEBUI_ROOT.is_dir()
    assert _webui_runtime_violations(WEBUI_ROOT) == []


@pytest.mark.spec("ARBORA-HOST-R013")
def test_census_fails_on_an_internal_runtime_import(tmp_path: Path) -> None:
    pkg = tmp_path / "webui_host"
    pkg.mkdir()
    (pkg / "ok.py").write_text(
        "from agent_utilities.api import x\n"
        "import arbora.fleet\n"
        "import epistemic_graph\n",
        encoding="utf-8",
    )
    assert _webui_runtime_violations(pkg) == []
    (pkg / "bad.py").write_text(
        "from meristem.core.paths import data_dir\n", encoding="utf-8"
    )
    assert _webui_runtime_violations(pkg) == [
        "webui_host/bad.py: meristem.core.paths",
        "webui_host/bad.py: meristem.core.paths.data_dir",
    ]


def _retired_imports(root: Path) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*.py")):
        for module in _imported_modules(path):
            if any(
                module == retired or module.startswith(f"{retired}.")
                for retired in RETIRED_RUNTIME_MODULES
            ):
                hits.append(f"{path.relative_to(root.parent)}: {module}")
    return sorted(set(hits))


def test_arbora_imports_no_retired_runtime_mcp_serving_module() -> None:
    assert _retired_imports(ARBORA_ROOT) == []


def test_retired_import_census_detects_each_retired_module(tmp_path: Path) -> None:
    pkg = tmp_path / "arbora"
    pkg.mkdir()
    for index, module in enumerate(RETIRED_RUNTIME_MODULES):
        (pkg / f"m{index}.py").write_text(f"import {module}\n", encoding="utf-8")
    assert len(_retired_imports(pkg)) == len(RETIRED_RUNTIME_MODULES)
