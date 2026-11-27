"""Projection evidence and the operator-only withheld count (ARBORA-OPS-R052.5)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.audit import audit_event
from arbora.api.invoke.audit_evidence import result_evidence
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import OpResult
from arbora.api.ops.operator_audit import handle_operator_audit_read, operations
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


class _OperatorChain:
    """The operator-only chain: the one place a withheld count is held."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    async def record_withheld(
        self, context: Any, *, withheld: int, request_id: str
    ) -> None:
        self.records[request_id] = {"request_id": request_id, "withheld": withheld}

    async def read(self, context: Any, *, request_id: str) -> dict[str, Any] | None:
        return self.records.get(request_id)


def _context(scopes: tuple[str, ...], chain: _OperatorChain | None) -> Any:
    listed = (_projection("sales", "data:sales"), _projection("hr", "data:hr"))
    caller = SimpleNamespace(
        effective_scopes=frozenset(scopes),
        principal="p",
        tenant="t",
        request_id="req-1",
    )
    services: dict[str, Any] = {"projection_reader": _Reader(listed)}
    if chain is not None:
        services["operator_audit"] = chain
    return SimpleNamespace(caller=caller, services=services)


@pytest.mark.spec("ARBORA-OPS-R052.5")
async def test_operator_reads_withheld_count_and_denied_caller_cannot() -> None:
    chain = _OperatorChain()
    ctx = _context(("data:sales",), chain)
    answer = await handle_query_projections(ctx, {}, _OP)  # type: ignore[arg-type]
    # The caller sees its own projection and no count of the withheld one.
    assert [p["boundary"] for p in answer["projections"]] == ["sales"]
    assert "withheld" not in json.dumps(answer) and "hr" not in json.dumps(answer)

    # The audit evidence of the call holds digests of the scope and references only.
    result = OpResult(value=answer)
    fields = result_evidence("query.projections", {}, result, ctx.caller)
    assert set(fields) == {
        "caller_scope_digest",
        "projection_refs_digest",
        "projection_count",
    }
    assert fields["projection_count"] == "1"
    event = audit_event(_OP, {}, ctx.caller, Surface.HTTP, "ok", evidence=fields)
    assert "withheld" not in json.dumps(event) and "m:hr" not in json.dumps(event)

    # The operator reads the count; the read declares the operator scope.
    (read_op,) = operations()
    assert read_op.scopes == frozenset({"audit:operator"})
    operator = _context(("audit:operator",), chain)
    record = await handle_operator_audit_read(
        operator, {"request_id": "req-1"}, read_op
    )
    assert record["value"]["withheld"] == 1

    # An unknown request and a missing chain refuse without a count.
    with pytest.raises(OperationRefused) as unknown:
        await handle_operator_audit_read(operator, {"request_id": "other"}, read_op)
    assert unknown.value.code == "NOT_FOUND"
    with pytest.raises(OperationRefused) as unbound:
        await handle_operator_audit_read(
            _context(("audit:operator",), None), {"request_id": "req-1"}, read_op
        )
    assert unbound.value.code == "UNAVAILABLE"
