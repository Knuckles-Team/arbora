"""Import fence for the access-operation service."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SERVICE = Path(__file__).resolve().parents[2] / "arbora" / "access" / "service.py"
_RETIRED_RUNTIME_PACKAGE = "agent_utilities"


def _retired_runtime_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(
                alias.name
                for alias in node.names
                if alias.name == _RETIRED_RUNTIME_PACKAGE
                or alias.name.startswith(f"{_RETIRED_RUNTIME_PACKAGE}.")
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == _RETIRED_RUNTIME_PACKAGE or node.module.startswith(
                f"{_RETIRED_RUNTIME_PACKAGE}."
            ):
                imports.add(node.module)
    return imports


@pytest.mark.spec("ARBORA-HOST-R007.2.3")
def test_service_imports_no_agent_runtime_module() -> None:
    """Access operations stay independent of the retired harness package."""
    assert _retired_runtime_imports(_SERVICE) == set()


@pytest.mark.spec("ARBORA-HOST-R007.2.3")
def test_service_import_fence_detects_a_retired_runtime_import(tmp_path: Path) -> None:
    planted = tmp_path / "service.py"
    planted.write_text(
        "from agent_utilities.runtime import deprecated_service\n", encoding="utf-8"
    )

    assert _retired_runtime_imports(planted) == {"agent_utilities.runtime"}
