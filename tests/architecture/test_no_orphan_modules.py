"""The process locks have one owner and no orphan twin (ARBORA-DEPLOY-R600.3)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"


@pytest.mark.spec("ARBORA-HOST-R039.10")
@pytest.mark.spec("ARBORA-DEPLOY-R600.3")
def test_no_host_lock_module() -> None:
    assert not (ARBORA_ROOT / "deployment" / "host_lock.py").exists()
    importers = [
        path.relative_to(ARBORA_ROOT).as_posix()
        for path in ARBORA_ROOT.rglob("*.py")
        if "host_lock" in path.read_text(encoding="utf-8")
        and any(
            "host_lock" in (alias.name, getattr(node, "module", None) or "")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        )
    ]
    assert importers == []


@pytest.mark.spec("ARBORA-DEPLOY-R600.3")
def test_the_engine_spawn_guard_has_one_production_caller() -> None:
    callers = {
        path.relative_to(ARBORA_ROOT).as_posix()
        for path in ARBORA_ROOT.rglob("*.py")
        if "engine_spawn_guard(" in path.read_text(encoding="utf-8")
    }
    assert callers == {"deployment/engine_lock.py", "mcp_server/composition.py"}
