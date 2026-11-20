"""Static ownership checks for the Arbora messaging boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_MESSAGING_MODULES = ("router.py", "registry.py", "service.py")


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


@pytest.mark.spec("ARBORA-MESSAGING-R006")
def test_supervision_imports_only_public_adapter_contract() -> None:
    """Router, registry, and service remain Arbora-owned, not Meristem clients."""
    source_root = Path(__file__).parents[2] / "arbora" / "messaging"
    imported = {
        module
        for filename in _MESSAGING_MODULES
        for module in _imported_modules(source_root / filename)
    }

    meristem_imports = sorted(
        module
        for module in imported
        if module == "meristem" or module.startswith("meristem.")
    )
    assert not meristem_imports, (
        "Arbora messaging supervision must not import Meristem: "
        f"{', '.join(meristem_imports)}"
    )
