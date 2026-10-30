"""Focused contract for the ARBORA-OPS-R022.1 hosted work operations.

Exercises the handler directly via ``tests/api/_ops_support``'s shared
context builder; full-registry wiring is covered separately by
``test_registry_factory.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import work
from arbora.api.registry import Effect, Registry, Surface
from tests.api._ops_support import service_context


class _FakeRunner:
    def __init__(self) -> None:
        self.items = {"item-1": {"item_id": "item-1", "title": "triage a ticket"}}
        self.offers: list[dict[str, Any]] = []

    async def list_items(
        self, *, tenant: str, principal: str, cursor: str | None, limit: int
    ) -> dict[str, Any]:
        return {"tenant": tenant, "items": list(self.items.values())[:limit]}

    async def get_item(
        self, item_id: str, *, tenant: str, principal: str
    ) -> dict[str, Any] | None:
        item = self.items.get(item_id)
        if item is None:
            return None
        return {**item, "tenant": tenant}

    async def create_offer(
        self,
        item_id: str,
        *,
        terms: dict[str, Any],
        tenant: str,
        principal: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        offer = {
            "item_id": item_id,
            "terms": terms,
            "tenant": tenant,
            "principal": principal,
            "idempotency_key": idempotency_key,
        }
        self.offers.append(offer)
        return offer


def _context(**services: object):
    return service_context(tenant="tenant:one", principal="p:one", **services)


def test_work_ops_declare_exact_scopes_and_effects() -> None:
    registry = Registry(work.operations())
    assert len(registry) == 3
    list_op = registry["work.items.list"]
    get_op = registry["work.items.get"]
    offer_op = registry["work.offers.create"]
    assert list_op.scopes == frozenset({"work:read"})
    assert get_op.scopes == frozenset({"work:read"})
    assert offer_op.scopes == frozenset({"work:write"})
    assert list_op.effect is Effect.READ
    assert offer_op.effect is Effect.WRITE
    assert Surface.MCP in list_op.surfaces
    assert all(op.examples for op in registry)


@pytest.mark.asyncio
async def test_items_list_handler_reports_unbound_runner_closed() -> None:
    op = work.operations()[0]
    with pytest.raises(RuntimeError, match="not bound"):
        await work.handle_work(_context(), {"cursor": None, "limit": 50}, op)


@pytest.mark.asyncio
async def test_items_list_handler_scopes_to_caller_tenant() -> None:
    runner = _FakeRunner()
    op = work.operations()[0]
    result = await work.handle_work(
        _context(work_runner=runner), {"cursor": None, "limit": 50}, op
    )
    assert result == {
        "value": {"tenant": "tenant:one", "items": list(runner.items.values())}
    }


@pytest.mark.asyncio
async def test_items_get_handler_reports_missing_item() -> None:
    runner = _FakeRunner()
    op = work.operations()[1]
    with pytest.raises(LookupError, match="not found"):
        await work.handle_work(_context(work_runner=runner), {"item_id": "absent"}, op)


@pytest.mark.asyncio
async def test_items_get_handler_reads_back_bound_item() -> None:
    runner = _FakeRunner()
    op = work.operations()[1]
    result = await work.handle_work(
        _context(work_runner=runner), {"item_id": "item-1"}, op
    )
    assert result == {
        "value": {
            "item_id": "item-1",
            "title": "triage a ticket",
            "tenant": "tenant:one",
        }
    }


@pytest.mark.asyncio
async def test_offers_create_handler_requires_idempotency_key() -> None:
    runner = _FakeRunner()
    op = work.operations()[2]
    context = _context(work_runner=runner)
    context.idempotency_key = None
    with pytest.raises(ValueError, match="Idempotency-Key"):
        await work.handle_work(context, {"item_id": "item-1", "terms": {}}, op)


@pytest.mark.asyncio
async def test_offers_create_handler_records_a_bound_offer() -> None:
    runner = _FakeRunner()
    op = work.operations()[2]
    result = await work.handle_work(
        _context(work_runner=runner),
        {"item_id": "item-1", "terms": {"rate": "standard"}},
        op,
    )
    assert result == {
        "value": {
            "item_id": "item-1",
            "terms": {"rate": "standard"},
            "tenant": "tenant:one",
            "principal": "p:one",
            "idempotency_key": "key-1",
        }
    }
    assert runner.offers == [result["value"]]


# R022.1 unbound: tenant filtering is implemented by the test double, not proven in
# production code, and the row requires visibility to the bound principal.
@pytest.mark.asyncio
async def test_offer_write_needs_declared_scope_and_offers_stay_with_their_tenant() -> (
    None
):
    from arbora.api.invoke.steps import require_scopes
    from tests.api._support import make_verified_caller

    offer_op = work.operations()[2]
    writer = make_verified_caller("work:write")
    reader = make_verified_caller("work:read")
    assert require_scopes(offer_op, writer()) is None
    refusal = require_scopes(offer_op, reader())
    assert refusal is not None and refusal.code == "SCOPE_REQUIRED"

    class _BoundRunner(_FakeRunner):
        async def get_item(self, item_id, *, tenant, principal):
            offers = [
                o
                for o in self.offers
                if o["tenant"] == tenant and o["principal"] == principal
            ]
            return {"item_id": item_id, "offers": offers} if offers else None

    runner = _BoundRunner()
    await work.handle_work(
        service_context(tenant="tenant:one", principal="p:one", work_runner=runner),
        {"item_id": "item-1", "terms": {}},
        offer_op,
    )
    get_op = work.operations()[1]
    own = await work.handle_work(
        service_context(tenant="tenant:one", principal="p:one", work_runner=runner),
        {"item_id": "item-1"},
        get_op,
    )
    assert len(own["value"]["offers"]) == 1
    # Same tenant, different principal: the offer is not visible.
    with pytest.raises(LookupError):
        await work.handle_work(
            service_context(
                tenant="tenant:one", principal="p:two", work_runner=runner
            ),
            {"item_id": "item-1"},
            get_op,
        )
    # Different tenant, same principal id: not visible either.
    with pytest.raises(LookupError):
        await work.handle_work(
            service_context(
                tenant="tenant:two", principal="p:one", work_runner=runner
            ),
            {"item_id": "item-1"},
            get_op,
        )
