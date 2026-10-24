"""Arbora never imports the agent-utilities provider module (ARBORA-HOST-R001.10).

A whole-repository static scan, so the pull-request fast subset always runs it
(``scripts/pr_static_scope.py``). Discovery itself goes through the connector
SDK's ``installed_connectors()``; see ``tests/test_installed_content.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.10")

_ARBORA = Path(__file__).resolve().parents[2] / "arbora"


def test_arbora_never_imports_the_agent_utilities_provider_module() -> None:
    offenders = []
    for path in sorted(_ARBORA.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            if any(name == "agent_utilities.core.providers" for name in names):
                offenders.append(path.relative_to(_ARBORA).as_posix())
    assert offenders == []
