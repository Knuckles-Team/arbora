"""ARBORA-FLEET-R006.5: no harvest or hydration code remains.

The closing audit reads the whole shipped ``arbora`` package, not only the
multiplexer, and asserts the call-site baseline of ``ARBORA-FLEET-R006.1`` is
zero: no function, class, attribute or call is named for the removed skill and
prompt body harvest.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import arbora

_REMOVED = (
    "_harvest_resource_bodies",
    "_harvest_deadline",
    "_harvest_error_reason",
    "_harvest_probe_results",
    "_resolve_via_local_catalog_or_harvest",
    "_SKILL_HARVEST_SPEC",
    "_PROMPT_HARVEST_SPEC",
    "_ResourceHarvestSpec",
)


def _harvest_sites() -> dict[str, int]:
    root = Path(arbora.__file__).resolve().parent
    sites: dict[str, int] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = node.name
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Attribute):
                name = node.attr
            else:
                continue
            count += name in _REMOVED
        if count:
            sites[path.relative_to(root).as_posix()] = count
    return sites


@pytest.mark.spec("ARBORA-FLEET-R006")
@pytest.mark.spec("ARBORA-FLEET-R006.5")
def test_baseline_is_zero() -> None:
    assert _harvest_sites() == {}
