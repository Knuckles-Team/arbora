"""ARBORA-HOST-R039.11: only host composition builds a ``RuntimeConfig``.

A view built at a call site reads the process environment and silently ignores the
overrides of the host's own view, so every unit receives the host's ``config`` by
argument. The one place that builds the host's view is the host composition root; a
one-shot command line entry is its own composition root and may build its own.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"

#: The host composition root and the one-shot entries that compose their own process.
COMPOSITION_ROOTS = frozenset(
    {
        "mcp_server/host_context.py",
        "identity_cli.py",
        "mcp_server/production_ops_cli.py",
        "deployment/doctor.py",
        "deployment/production_ops.py",
        "deployment/provision.py",
        # Singleton service pending its host-state move (ARBORA-HOST-R039.13).
        "messaging/service.py",
    }
)


def _constructions(source_file: Path) -> list[int]:
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "RuntimeConfig")
            or (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "RuntimeConfig"
            )
        )
    ]


@pytest.mark.spec("ARBORA-HOST-R039.11")
def test_no_runtime_config_is_constructed_outside_host_composition() -> None:
    offenders = [
        f"{path.relative_to(ARBORA_ROOT).as_posix()}:{line}"
        for path in sorted(ARBORA_ROOT.rglob("*.py"))
        if path.relative_to(ARBORA_ROOT).as_posix() not in COMPOSITION_ROOTS
        for line in _constructions(path)
    ]
    assert offenders == []


@pytest.mark.spec("ARBORA-HOST-R039.11")
def test_no_module_level_runtime_config_remains() -> None:
    from arbora.core import settings

    assert not hasattr(settings, "runtime_config")
    assert not hasattr(settings, "_write_lock")
