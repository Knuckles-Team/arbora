"""Find every writer of a lifecycle field inside a package directory.

A machine's transition function is the only writer of its state field
(organization constitution, explicit state machines). ``field_writers`` returns
each place that assigns one of the given field names: an attribute store, a
subscript store with a constant key, a dict-literal key of ``update=``, a keyword
of ``model_copy``/``replace``/``_successor``, or ``setattr``.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_COPIERS = frozenset({"model_copy", "replace", "_successor", "copy"})


def _call_name(node: ast.Call) -> str:
    func = node.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _constant_in(node: ast.expr | None, fields: frozenset[str]) -> bool:
    return isinstance(node, ast.Constant) and node.value in fields


def _store(target: ast.expr, fields: frozenset[str]) -> bool:
    if isinstance(target, ast.Attribute):
        return target.attr in fields
    return isinstance(target, ast.Subscript) and _constant_in(target.slice, fields)


def _update_has(value: ast.expr, fields: frozenset[str]) -> bool:
    return isinstance(value, ast.Dict) and any(
        _constant_in(key, fields) for key in value.keys
    )


def _assigns(node: ast.AST, fields: frozenset[str]) -> bool:
    if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
        return False
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    return any(_store(target, fields) for target in targets)


def _copies(node: ast.Call, fields: frozenset[str]) -> bool:
    if any(kw.arg in fields for kw in node.keywords):
        return True
    return any(
        _update_has(kw.value, fields) for kw in node.keywords if kw.arg == "update"
    )


def _calls(node: ast.AST, fields: frozenset[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    name = _call_name(node)
    if name == "setattr":
        return len(node.args) >= 2 and _constant_in(node.args[1], fields)
    return name in _COPIERS and _copies(node, fields)


def _hits(node: ast.AST, fields: frozenset[str]) -> bool:
    return _assigns(node, fields) or _calls(node, fields)


def field_writers(directory: str, fields: set[str]) -> set[tuple[str, str]]:
    """``(relative file, enclosing function)`` of every write of ``fields`` under ``directory``."""
    wanted = frozenset(fields)
    found: set[tuple[str, str]] = set()
    for path in sorted((ROOT / directory).rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if _hits(node, wanted):
                    found.add((path.relative_to(ROOT).as_posix(), function.name))
    return found
