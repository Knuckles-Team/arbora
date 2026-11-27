"""Dependency floors and the scion-sdk import surface (ARBORA-HOST-R040)."""

from __future__ import annotations

import ast
import importlib
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PIN_LAG_MODULES = {"scion_sdk.meeting.client"}


def _dependencies() -> list[str]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return list(data["project"]["dependencies"])


def _is_sdk(module: str | None) -> bool:
    return bool(module) and module.split(".")[0] == "scion_sdk"  # type: ignore[union-attr]


def _node_sdk_imports(node: ast.AST) -> set[tuple[str, str]]:
    if isinstance(node, ast.ImportFrom) and node.level == 0 and _is_sdk(node.module):
        return {(node.module or "", alias.name) for alias in node.names}
    if isinstance(node, ast.Import):
        return {(a.name, "") for a in node.names if _is_sdk(a.name)}
    return set()


def _sdk_imports() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / "arbora").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            found |= _node_sdk_imports(node)
    return found


@pytest.mark.spec("ARBORA-HOST-R040.3")
def test_every_scion_sdk_import_resolves_on_the_pinned_release() -> None:
    missing: list[str] = []
    for module, name in sorted(_sdk_imports()):
        if module in PIN_LAG_MODULES:
            continue
        try:
            loaded = importlib.import_module(module)
        except ImportError:
            missing.append(module)
            continue
        if name and not hasattr(loaded, name):
            try:
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                missing.append(f"{module}:{name}")
    assert not missing, "scion_sdk lacks: " + ", ".join(missing)


@pytest.mark.spec("ARBORA-HOST-R040.3")
def test_no_import_reaches_a_private_scion_sdk_module() -> None:
    private = sorted(
        module
        for module, _name in _sdk_imports()
        if any(part.startswith("_") for part in module.split("."))
    )
    assert private == []


@pytest.mark.spec("ARBORA-HOST-R040.3")
def test_arbora_declares_one_scion_sdk_floor_with_the_transports_it_serves() -> None:
    sdk = [d for d in _dependencies() if d.startswith("scion-sdk")]
    assert len(sdk) == 1
    assert "a2a" in sdk[0]
    assert "mcp" in sdk[0]
    assert ">=" in sdk[0]
    assert "<" in sdk[0]


_RETIRED = "agent" + "_utilities"
_SCANNED_ROOTS = ("arbora", "tests", "docs", "scripts", ".github")
_SCANNED_FILES = ("pyproject.toml", "README.md", "AGENTS.md")


def _scanned_files() -> list[Path]:
    found = [ROOT / name for name in _SCANNED_FILES if (ROOT / name).is_file()]
    for top in _SCANNED_ROOTS:
        base = ROOT / top
        if base.is_dir():
            found.extend(
                path
                for path in base.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path != Path(__file__).resolve()
                and path.suffix not in {".pyc", ".png", ".ico", ".woff2", ".whl"}
            )
    return found


@pytest.mark.spec("ARBORA-HOST-R040.1")
def test_no_agent_utilities_reference_remains() -> None:
    """The retired harness package is named nowhere in code, config, fixtures or docs."""
    spellings = (_RETIRED, _RETIRED.replace("_", "-"), _RETIRED.upper())
    offenders: list[str] = []
    for path in _scanned_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if any(spelling in text for spelling in spellings):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


@pytest.mark.spec("ARBORA-HOST-R040.1")
def test_pins_match_the_adopted_releases() -> None:
    dependencies = _dependencies()
    assert "meristem>=6.0.0,<7.0.0" in dependencies
    sdk = [d for d in dependencies if d.startswith("scion-sdk")]
    assert len(sdk) == 1
    assert sdk[0].startswith("scion-sdk[a2a,mcp]>=0.11")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "meristem"\nversion = "6.' in lock
