"""Every refusal code a served operation raises is a closed ``ArboraErrorCode`` member.

A code outside the enum used to surface as an untyped HTTP 500. The scan reads
the package source, so a new ``OperationRefused("NEW_CODE")`` fails here until
the code has its wire status and JSON-RPC mapping.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from arbora.api.errors import (
    _ARBORA_STATUS,
    _RPC_CODES,
    ArboraErrorCode,
    ArboraRefusal,
    a2a_error_status,
    to_envelope,
)

ROOT = Path(__file__).resolve().parents[2] / "arbora"
CODE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*\Z")
# Constructors whose first argument is a wire code.
_WIRE_CALLS = {"OperationRefused", "ManifestRefused"}
# Per-module refusal helpers in the served operations; write_back prefixes its codes.
_OPS_HELPERS = {"_refuse", "_refused"}
# Confirmation is a result on A2A, never an error.
_RESULT_CODES = {
    ArboraErrorCode.CONFIRMATION_REQUIRED,
    ArboraErrorCode.STEP_UP_REQUIRED,
}


def _literal(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and CODE.match(node.value)
    ):
        return node.value
    return None


def _dict_values(node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Dict):
        for value in node.values:
            code = _literal(value)
            if code is not None:
                yield code


def _call_codes(node: ast.Call, in_ops: bool, prefix: str) -> list[str]:
    """The wire codes one call names: constructor, ops helper or ``refusals=``."""
    name = getattr(node.func, "id", getattr(node.func, "attr", ""))
    codes: list[str] = []
    helper = in_ops and name in _OPS_HELPERS
    first = _literal(node.args[0]) if node.args else None
    if first is not None and (helper or name in _WIRE_CALLS):
        codes.append(prefix + first if helper else first)
    for keyword in node.keywords:
        if keyword.arg == "refusals":
            codes.extend(_dict_values(keyword.value))
    return codes


def _assign_codes(node: ast.Assign) -> list[str]:
    named = any(isinstance(t, ast.Name) and t.id == "_NOT_FOUND" for t in node.targets)
    return list(_dict_values(node.value)) if named else []


def _node_codes(node: ast.AST, in_ops: bool, prefix: str) -> list[str]:
    if isinstance(node, ast.Call):
        return _call_codes(node, in_ops, prefix)
    if isinstance(node, ast.Assign):
        return _assign_codes(node)
    return []


def _raised_codes() -> dict[str, str]:
    """Each wire code named in source, with the first place it appears."""
    found: dict[str, str] = {}
    for path in sorted(ROOT.rglob("*.py")):
        in_ops = "api/ops" in path.as_posix()
        prefix = "WRITE_BACK_" if path.name == "write_back.py" else ""
        where = path.relative_to(ROOT.parent)
        for node in ast.walk(ast.parse(path.read_text())):
            for code in _node_codes(node, in_ops, prefix):
                found.setdefault(code, f"{where}:{node.lineno}")
    return found


_NAMED_TABLES = {"_CHILD_REFUSALS", "ADMISSION_CODES", "ARBORA_CODE"}


def _table_codes(node: ast.Assign) -> set[str]:
    codes: set[str] = set()
    for leaf in ast.walk(node.value):
        if isinstance(leaf, ast.Dict):
            codes.update(_dict_values(leaf))
        elif isinstance(leaf, ast.Constant) and _literal(leaf):
            codes.add(leaf.value)
    return codes


def _class_attribute_codes(node: ast.ClassDef) -> set[str]:
    return {
        item.value.value
        for item in node.body
        if isinstance(item, ast.Assign) and _literal(item.value)
    }


def _named_codes(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Assign) and any(
        isinstance(t, ast.Name) and t.id in _NAMED_TABLES for t in node.targets
    ):
        return _table_codes(node)
    if isinstance(node, ast.ClassDef) and node.name == "SessionNotOwned":
        return _class_attribute_codes(node)
    return set()


def _named_tables() -> set[str]:
    """Codes held in constant tables or class attributes the call scan cannot see."""
    codes: set[str] = set()
    for path in sorted(ROOT.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            codes |= _named_codes(node)
    return codes


@pytest.mark.spec("ARBORA-OPS-R010")
def test_every_raised_refusal_code_is_a_closed_enum_member() -> None:
    members = {member.value for member in ArboraErrorCode}
    raised = _raised_codes()
    assert len(raised) > 40, "the scan found too few codes to be reading the package"
    unmapped = {code: where for code, where in raised.items() if code not in members}
    assert not unmapped, f"codes raised but absent from ArboraErrorCode: {unmapped}"
    named = _named_tables()
    assert {"SESSION_NOT_OWNED", "TENANT_DENIED"} <= named
    assert named <= members, sorted(named - members)


@pytest.mark.spec("ARBORA-OPS-R010")
def test_every_member_has_an_http_status_and_a_json_rpc_code() -> None:
    assert set(_ARBORA_STATUS) == set(ArboraErrorCode)
    assert set(_RPC_CODES) == set(ArboraErrorCode) - _RESULT_CODES
    for code in ArboraErrorCode:
        assert 400 <= _ARBORA_STATUS[code] < 600


@pytest.mark.spec("ARBORA-OPS-R010")
@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("PATH_ESCAPE", 400),
        ("WRITE_CONFLICT", 409),
        ("PLAN_REQUIRED", 428),
        ("PAYLOAD_TOO_LARGE", 413),
        ("SESSION_NOT_OWNED", 404),
    ],
)
def test_previously_missing_codes_answer_their_own_status(
    code: str, status: int
) -> None:
    http, envelope = to_envelope(
        ArboraRefusal(ArboraErrorCode(code)),
        op="workspace.files.write",
        request_id="r1",
        registry_digest="a" * 64,
    )
    assert http == status
    assert envelope["error"]["code"] == code
    assert a2a_error_status(code)[1] == status


@pytest.mark.spec("ARBORA-OPS-R010")
def test_an_unmapped_code_is_a_typed_internal_with_the_code() -> None:
    class Refused:
        code = "NOT_YET_IN_THE_ENUM"
        details: dict[str, str] = {}

    http, envelope = to_envelope(
        Refused(), op="x.y", request_id="r1", registry_digest="a" * 64
    )
    assert http == 500
    assert envelope["error"]["code"] == "INTERNAL"
    assert envelope["error"]["source"] == "arbora"
    assert envelope["error"]["retryable"] is False
    assert envelope["error"]["details"] == {"unmapped_code": "NOT_YET_IN_THE_ENUM"}
    assert a2a_error_status("NOT_YET_IN_THE_ENUM") == (-32603, 500)
