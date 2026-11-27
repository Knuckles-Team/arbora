"""The installed engine client exposes every symbol Arbora imports (ARBORA-HOST-R002)."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ARBORA_ROOT = Path(__file__).resolve().parents[2] / "arbora"


def _is_engine(module: str) -> bool:
    return module == "medulla" or module.startswith("medulla.")


_PROBE_ERRORS = {"ImportError", "ModuleNotFoundError", "AttributeError"}


def _handler_names(handler: ast.ExceptHandler) -> set[str]:
    caught = handler.type
    items = caught.elts if isinstance(caught, ast.Tuple) else [caught]
    return {getattr(t, "id", getattr(t, "attr", "")) for t in items if t is not None}


def _is_probe(node: ast.AST) -> bool:
    return isinstance(node, ast.Try) and any(
        _handler_names(h) & _PROBE_ERRORS for h in node.handlers
    )


def _body_lines(node: ast.Try) -> set[int]:
    return {
        n.lineno for child in node.body for n in ast.walk(child) if hasattr(n, "lineno")
    }


def _guarded(tree: ast.AST) -> set[int]:
    """Line numbers inside a ``try`` that catches ``ImportError``.

    Those imports are the declared pin-lag probes: they refuse when the symbol
    is absent, so the installed client need not carry them.
    """
    lines: set[int] = set()
    for node in ast.walk(tree):
        if _is_probe(node):
            lines |= _body_lines(node)  # type: ignore[arg-type]
    return lines


def _is_engine_import_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call) or not node.args:
        return False
    name = getattr(node.func, "attr", getattr(node.func, "id", ""))
    first = node.args[0]
    return (
        name == "import_module"
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and _is_engine(first.value)
    )


def _from_import(node: ast.ImportFrom) -> tuple[set[tuple[str, str]], set[str]]:
    if node.level != 0 or not node.module or not _is_engine(node.module):
        return set(), set()
    names = {(node.module, a.name) for a in node.names if a.name != "*"}
    return names, {node.module}


def _node_imports(node: ast.AST) -> tuple[set[tuple[str, str]], set[str]]:
    if isinstance(node, ast.ImportFrom):
        return _from_import(node)
    if isinstance(node, ast.Import):
        return set(), {a.name for a in node.names if _is_engine(a.name)}
    if _is_engine_import_call(node):
        return set(), {node.args[0].value}  # type: ignore[attr-defined]
    return set(), set()


def _file_imports(path: Path) -> tuple[set[tuple[str, str]], set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    guarded = _guarded(tree)
    symbols: set[tuple[str, str]] = set()
    modules: set[str] = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", 0) in guarded:
            continue
        found_symbols, found_modules = _node_imports(node)
        symbols |= found_symbols
        modules |= found_modules
    return symbols, modules


def _imported_symbols() -> tuple[set[tuple[str, str]], set[str]]:
    """Every ``(module, name)`` and bare module the code imports from the client."""
    symbols: set[tuple[str, str]] = set()
    modules: set[str] = set()
    for path in sorted(ARBORA_ROOT.rglob("*.py")):
        found_symbols, found_modules = _file_imports(path)
        symbols |= found_symbols
        modules |= found_modules
    return symbols, modules


def _missing(symbols: set[tuple[str, str]], modules: set[str]) -> list[str]:
    missing: list[str] = []
    for module in sorted(modules):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(f"{module}:<module>")
    for module, name in sorted(symbols):
        try:
            loaded = importlib.import_module(module)
        except ImportError:
            continue
        if hasattr(loaded, name):
            continue
        try:
            importlib.import_module(f"{module}.{name}")
        except ImportError:
            missing.append(f"{module}:{name}")
    return missing


@pytest.mark.spec("ARBORA-HOST-R002")
def test_every_imported_client_symbol_exists() -> None:
    symbols, modules = _imported_symbols()
    assert symbols, "the scan found no engine-client import to check"
    missing = _missing(symbols, modules)
    assert not missing, "installed medulla lacks: " + ", ".join(missing)


@pytest.mark.spec("ARBORA-HOST-R002")
def test_a_renamed_symbol_is_reported_as_module_and_symbol() -> None:
    missing = _missing({("medulla", "no_such_symbol_xyz")}, {"medulla"})
    assert missing == ["medulla:no_such_symbol_xyz"]
