"""Projection scope behavior: exact and partial cover (ARBORA-OPS-R052.2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import query
from arbora.api.ops.projections import (
    SchemaProjection,
    handle_query_projections,
    scope_covers,
)

pytestmark = pytest.mark.spec("ARBORA-OPS-R052.2")

_OP = {op.id: op for op in query.operations()}["query.projections"]

SALES = SchemaProjection(
    boundary="sales",
    required_scopes=("data:sales", "query:read"),
    tables=("orders", "invoices"),
    mapping_ref="mapping:sales",
)
HR = SchemaProjection(
    boundary="hr",
    required_scopes=("data:hr",),
    tables=("people",),
    mapping_ref="mapping:hr",
)


class _Reader:
    def __init__(self, listed: tuple[SchemaProjection, ...]) -> None:
        self.listed = listed
        self.calls = 0

    async def list(self, context: object) -> tuple[SchemaProjection, ...]:
        self.calls += 1
        return self.listed


def _context(scopes: set[str], reader: _Reader | None) -> SimpleNamespace:
    services = {} if reader is None else {"projection_reader": reader}
    caller = SimpleNamespace(tenant="tenant-a", effective_scopes=frozenset(scopes))
    return SimpleNamespace(caller=caller, services=services)


async def test_exact_cover_and_partial_cover() -> None:
    # Exact cover: every required scope is held, nothing more needed.
    assert scope_covers({"data:sales", "query:read"}, SALES)
    # One required scope missing: not covered, however many others are held.
    assert not scope_covers({"query:read", "data:hr", "admin"}, SALES)
    assert not scope_covers(set(), HR)

    reader = _Reader((SALES, HR))
    context = _context({"query:read", "data:sales"}, reader)
    result = await handle_query_projections(context, {}, _OP)
    assert [item["boundary"] for item in result["projections"]] == ["sales"]
    assert "hr" not in repr(result) and "people" not in repr(result)

    both = await handle_query_projections(
        _context({"query:read", "data:sales", "data:hr"}, reader), {}, _OP
    )
    assert [item["boundary"] for item in both["projections"]] == ["sales", "hr"]


async def test_no_cover_and_no_boundary_refuse_without_detail() -> None:
    with pytest.raises(OperationRefused) as denied:
        await handle_query_projections(
            _context({"query:read"}, _Reader((SALES, HR))), {}, _OP
        )
    assert denied.value.code == "SUBJECT_ACCESS_DENIED"
    assert "hr" not in repr(denied.value.details) and "2" not in repr(
        denied.value.details
    )
    with pytest.raises(OperationRefused) as empty:
        await handle_query_projections(_context({"query:read"}, _Reader(())), {}, _OP)
    assert empty.value.code == "UNAVAILABLE"
    with pytest.raises(OperationRefused) as unbound:
        await handle_query_projections(_context({"query:read"}, None), {}, _OP)
    assert unbound.value.code == "UNAVAILABLE"
