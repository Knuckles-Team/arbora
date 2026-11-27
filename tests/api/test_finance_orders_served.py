"""Order confirmation waits for the engine admission receipt (ARBORA-OPS-R070, pin-lag)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import finance_orders
from arbora.api.ops.registry_factory import get_registry
from tests.api._lane_double import EngineDouble, context

D = "a" * 64
_PARAMS = {"proposal_id": "p1", "admission_digest": D, "approver_set_digest": "b" * 64}


@pytest.mark.spec("ARBORA-OPS-R070")
def test_the_operation_is_absent_until_the_engine_publishes_the_receipt() -> None:
    assert finance_orders.receipt_published() is False
    assert finance_orders.operations() == ()
    assert get_registry().get("order.proposal.confirm") is None


@pytest.mark.spec("ARBORA-OPS-R070")
async def test_the_handler_refuses_naming_the_missing_receipt_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineDouble()
    with pytest.raises(OperationRefused) as refused:
        await finance_orders.handle_order_proposal_confirm(
            context(client, monkeypatch),
            _PARAMS,
            None,  # type: ignore[arg-type]
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "FinanceOrderReceiptGet"
    assert client.sent == []


@pytest.mark.spec("ARBORA-OPS-R070")
async def test_confirmation_binds_digest_and_refuses_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(finance_orders, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"FinanceOrderReceiptGet"},
    )

    async def send(client, method, params, graph, **kw):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)
    client = EngineDouble(
        FinanceOrderReceiptGet={
            "receipt_id": "rcpt-1",
            "receipt_digest": D,
            "receipt": {"proposal_id": "p1"},
        }
    )
    ctx = context(client, monkeypatch)
    issued = await finance_orders.handle_order_proposal_confirm(ctx, _PARAMS, None)  # type: ignore[arg-type]
    assert issued["value"]["admission_digest"] == D
    assert issued["value"]["receipt_id"] == "rcpt-1"
    assert issued["value"]["receipt_digest"]
    stale = {**_PARAMS, "admission_digest": "c" * 64}
    with pytest.raises(OperationRefused) as refused:
        await finance_orders.handle_order_proposal_confirm(ctx, stale, None)  # type: ignore[arg-type]
    assert refused.value.code == "PLAN_STALE"
    # A refused confirmation issues no receipt and calls no provider: the only
    # engine call is the receipt read.
    assert {method for method, *_ in client.sent} == {"FinanceOrderReceiptGet"}


@pytest.mark.spec("ARBORA-OPS-R070")
async def test_an_absent_unpromoted_or_foreign_proposal_answers_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    from arbora.api.errors import EngineRefusal

    monkeypatch.setattr(finance_orders, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"FinanceOrderReceiptGet"},
    )

    async def send(client, method, params, graph, **kw):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)
    codes = set()
    for proposal in ("absent", "unpromoted", "foreign"):
        client = EngineDouble(
            FinanceOrderReceiptGet=EngineRefusal("ORDER_RECEIPT_NOT_FOUND")
        )
        with pytest.raises(EngineRefusal) as refused:
            await finance_orders.handle_order_proposal_confirm(
                context(client, monkeypatch),
                {**_PARAMS, "proposal_id": proposal},
                None,  # type: ignore[arg-type]
            )
        codes.add((refused.value.code, str(refused.value)))
    assert len(codes) == 1 and codes.pop()[0] == "ORDER_RECEIPT_NOT_FOUND"
