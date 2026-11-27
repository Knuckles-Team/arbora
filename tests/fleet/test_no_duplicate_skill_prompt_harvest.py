"""ARBORA-A2A-R003: the deployed arbora package harvests fleet skills
and prompts through exactly one owner.

meristem ships its own legacy MCP multiplexer
(``the retired harness multiplexer`` / ``the retired shared multiplexer``)
with its own skill/prompt body-harvest path. arbora owns fleet composition
and discovery (``arbora-repository-development`` skill, "arbora.fleet |
MCP child lifecycle, catalog discovery..."); its served package must never
import that legacy path, and
must define the skill/prompt catalog functions
(:func:`~arbora.fleet.child_payload_bounds._bounded_skill_catalog`,
:func:`~arbora.fleet.child_payload_bounds._bounded_prompt_catalog`) in exactly one
module. This is a static, source-level absence check: what the package's
own source never imports can never load as a side effect of running it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import arbora

#: The retired harness package name, spelled so no source line contains it.
RETIRED = "agent_" + "utilities"

_FORBIDDEN_MODULES = (
    f"{RETIRED}.mcp.multiplexer",
    f"{RETIRED}.mcp.shared_multiplexer",
)

_HARVEST_FUNCTIONS = (
    "_bounded_prompt_catalog",
    "_bounded_skill_catalog",
)


def _package_root() -> Path:
    return Path(arbora.__file__).resolve().parent


def _iter_source_files() -> list[Path]:
    return sorted(_package_root().rglob("*.py"))


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


@pytest.mark.spec("ARBORA-A2A-R003")
@pytest.mark.spec("ARBORA-OPS-R003")
def test_no_source_file_imports_the_legacy_au_multiplexer() -> None:
    offenders: dict[str, set[str]] = {}
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = {
            module
            for module in _imported_module_names(tree)
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in _FORBIDDEN_MODULES
            )
        }
        if hits:
            offenders[path.relative_to(_package_root()).as_posix()] = hits
    assert not offenders, (
        "arbora source imports meristem's legacy MCP multiplexer, "
        f"duplicating the skill/prompt harvest path: {offenders}"
    )


def test_skill_and_prompt_body_harvest_has_exactly_one_owner_module() -> None:
    owners: dict[str, list[str]] = {}
    for path in _iter_source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        defined = sorted(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name in _HARVEST_FUNCTIONS
        )
        if defined:
            owners[path.relative_to(_package_root()).as_posix()] = defined
    assert owners == {"fleet/child_payload_bounds.py": list(_HARVEST_FUNCTIONS)}, (
        f"expected exactly one skill/prompt harvest owner, found: {owners}"
    )
