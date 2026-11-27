"""Every ``Decide`` question kind arbora sends is an engine snake_case kind (ARBORA-OPS-R875, R870)."""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from arbora.api.ops._decide import decide_with, require_wire_kind
from arbora.pin_lag.gates import GATE_QUESTIONS
from tests.question_kinds import ENGINE_QUESTION_KINDS

_ROOT = Path(__file__).resolve().parents[2] / "arbora"


def _text(node: ast.AST | None) -> str | None:
    return (
        node.value
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
        else None
    )


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _tuple_kind(node: ast.AST) -> str | None:
    """The kind of a ``(question_id, kind, safety)`` tuple."""
    elts = node.elts if isinstance(node, ast.Tuple) else []
    return _text(elts[1]) if len(elts) > 1 else None


#: Each Decide request builder and how its question kind is read from the call arguments.
_KIND_READERS: dict[str, tuple[int, Callable[[ast.AST], str | None]]] = {
    "decide_record": (1, _text),
    "decide_with": (2, _text),
    "question_kind_served": (0, _text),
    "decide_request": (1, _tuple_kind),
}


def _kind_of(call: ast.Call) -> str | None:
    index, read = _KIND_READERS.get(_call_name(call), (0, None))
    ok = read is not None and len(call.args) > index
    return read(call.args[index]) if ok and read else None


def _sent_kinds() -> dict[str, str]:
    """The literal question kinds source passes to the Decide request builders."""
    calls = (
        (path, node)
        for path in sorted(_ROOT.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
    )
    found = {
        f"{path.relative_to(_ROOT)}:{node.lineno}": _kind_of(node)
        for path, node in calls
    }
    return {where: kind for where, kind in found.items() if kind is not None}


@pytest.mark.spec("ARBORA-OPS-R875")
def test_every_kind_arbora_sends_is_in_the_engine_snake_case_set() -> None:
    sent = _sent_kinds()
    assert sent, "the scan found no Decide question kind"
    unknown = {
        where: kind for where, kind in sent.items() if kind not in ENGINE_QUESTION_KINDS
    }
    assert unknown == {}
    assert set(GATE_QUESTIONS.values()) <= ENGINE_QUESTION_KINDS


@pytest.mark.spec("ARBORA-OPS-R870")
async def test_a_camel_case_kind_never_reaches_the_wire() -> None:
    sent: list[str] = []

    async def send(method: str, params: dict[str, object]) -> object:
        sent.append(method)
        return {}

    for kind in ("SkillUpgrade", "ChangesetReview", "skill-upgrade", "", "_x"):
        with pytest.raises(ValueError, match="snake_case"):
            await decide_with(send, "tenant:one", kind)
    assert sent == []
    assert require_wire_kind("changeset_review") == "changeset_review"
