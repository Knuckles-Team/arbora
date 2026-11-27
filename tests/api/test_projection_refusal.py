"""Refusals and non-disclosure of uncovered projections (ARBORA-OPS-R052.4)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.audit import audit_event
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.projections import SchemaProjection, handle_query_projections
from arbora.api.registry import Surface

_OP = SimpleNamespace(id="query.projections", executor=SimpleNamespace(value="caller"))


def _projection(name: str, scope: str) -> SchemaProjection:
    return SchemaProjection(
        boundary=name, required_scopes=(scope,), tables=("t",), mapping_ref=f"m:{name}"
    )


class _Reader:
    def __init__(self, listed: tuple[SchemaProjection, ...]) -> None:
        self.listed = listed

    async def list(self, context: Any) -> tuple[SchemaProjection, ...]:
        return self.listed


def _context(scopes: tuple[str, ...], listed: tuple[SchemaProjection, ...]) -> Any:
    caller = SimpleNamespace(
        effective_scopes=frozenset(scopes),
        principal="p",
        tenant="t",
        request_id="r",
    )
    return SimpleNamespace(
        caller=caller, services={"projection_reader": _Reader(listed)}
    )


@pytest.mark.spec("ARBORA-OPS-R052.4")
async def test_refusals_and_hidden_projection_counts_are_absent_everywhere() -> None:
    visible, hidden = _projection("sales", "data:sales"), _projection("hr", "data:hr")
    # A partial cover returns only the covered projection; nothing names or counts the rest.
    answer = await handle_query_projections(
        _context(("data:sales",), (visible, hidden)), {}, _OP
    )
    assert [p["boundary"] for p in answer["projections"]] == ["sales"]
    assert "hr" not in json.dumps(answer) and "m:hr" not in json.dumps(answer)
    # The caller-readable audit event carries a digest of the arguments, not projections.
    caller = SimpleNamespace(principal="p", tenant="t", request_id="r")
    event = audit_event(_OP, {}, caller, Surface.HTTP, "OK")
    assert "hr" not in json.dumps(event)
    # No projection covered: SUBJECT_ACCESS_DENIED with no detail at all.
    with pytest.raises(OperationRefused) as denied:
        await handle_query_projections(_context((), (visible, hidden)), {}, _OP)
    assert denied.value.code == "SUBJECT_ACCESS_DENIED"
    assert not denied.value.details and "hr" not in str(denied.value.args)
    # The refusal is identical whether one or many projections are hidden.
    with pytest.raises(OperationRefused) as one:
        await handle_query_projections(_context((), (hidden,)), {}, _OP)
    assert (one.value.code, one.value.details) == (
        denied.value.code,
        denied.value.details,
    )
    # The engine returning no boundary labels is UNAVAILABLE.
    with pytest.raises(OperationRefused) as none:
        await handle_query_projections(_context(("data:sales",), ()), {}, _OP)
    assert none.value.code == "UNAVAILABLE"
