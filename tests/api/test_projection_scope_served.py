"""Two scopes see different projection sets; hidden projections never leak (ARBORA-OPS-R052.6)."""

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
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Surface
from tests.api.test_projection_evidence import _OperatorChain


def _projection(name: str, scope: str) -> SchemaProjection:
    return SchemaProjection(
        boundary=name, required_scopes=(scope,), tables=("t",), mapping_ref=f"m:{name}"
    )


class _Reader:
    def __init__(self, listed: tuple[SchemaProjection, ...]) -> None:
        self.listed = listed

    async def list(self, context: Any) -> tuple[SchemaProjection, ...]:
        return self.listed


def _context(
    scopes: tuple[str, ...], listed: tuple[SchemaProjection, ...], chain: Any = None
) -> Any:
    caller = SimpleNamespace(
        effective_scopes=frozenset(scopes), principal="p", tenant="t", request_id="r"
    )
    services: dict[str, Any] = {"projection_reader": _Reader(listed)}
    if chain is not None:
        services["operator_audit"] = chain
    return SimpleNamespace(caller=caller, services=services)


async def _surface(ctx: Any) -> dict[str, str]:
    """Everything a caller can read of one call: response, evidence and audit event."""
    op = get_registry()["query.projections"]
    answer = await handle_query_projections(ctx, {}, op)
    fields = result_evidence(
        "query.projections", {}, OpResult(value=answer), ctx.caller
    )
    event = audit_event(op, {}, ctx.caller, Surface.HTTP, "ok", evidence=fields)
    return {"response": json.dumps(answer), "event": json.dumps(event)}


@pytest.mark.spec("ARBORA-OPS-R052.6")
async def test_two_scopes_see_different_projection_sets() -> None:
    sales, hr, ops = (
        _projection("sales", "data:sales"),
        _projection("hr", "data:hr"),
        _projection("ops", "data:ops"),
    )
    only_sales = await handle_query_projections(
        _context(("data:sales",), (sales, hr)),
        {},
        None,  # type: ignore[arg-type]
    )
    both = await handle_query_projections(
        _context(("data:sales", "data:hr"), (sales, hr)),
        {},
        None,  # type: ignore[arg-type]
    )
    assert [p["boundary"] for p in only_sales["projections"]] == ["sales"]
    assert [p["boundary"] for p in both["projections"]] == ["sales", "hr"]

    # Varying only what the caller cannot see changes nothing it can read.
    base = await _surface(_context(("data:sales",), (sales, hr)))
    more_hidden = await _surface(_context(("data:sales",), (sales, hr, ops)))
    assert base == more_hidden

    # A caller with no covered projection is refused identically, however many hide.
    refusals = []
    for listed in ((hr,), (hr, ops)):
        with pytest.raises(OperationRefused) as refused:
            await handle_query_projections(
                _context(("data:sales",), listed),
                {},
                None,  # type: ignore[arg-type]
            )
        refusals.append((refused.value.code, dict(refused.value.details)))
    assert refusals[0] == refusals[1] == ("SUBJECT_ACCESS_DENIED", {})

    # The count of hidden projections is held by the operator chain only, and a
    # denied caller (no chain access in its context) reads nothing from it.
    chain = _OperatorChain()
    await handle_query_projections(
        _context(("data:sales",), (sales, hr, ops), chain),
        {},
        None,  # type: ignore[arg-type]
    )
    assert chain.records["r"]["withheld"] == 2
    (read_op,) = operations()
    denied = _context(("data:sales",), (sales,))
    with pytest.raises(OperationRefused) as unbound:
        await handle_operator_audit_read(denied, {"request_id": "r"}, read_op)
    assert unbound.value.code == "UNAVAILABLE"
    assert get_registry()["audit.operator.read"].scopes == frozenset({"audit:operator"})
