"""``market.performance`` forwards the engine projection verbatim (ARBORA-DATA-MARKET-R009.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import market_detail as detail
from tests.mcp_server._read_port_double import FakeReadPort

NO_OP: Any = None
PARAMS = {
    "range": "1Y",
    "method": "twr",
    "as_of": "2026-11-01T00:00:00Z",
    "known_at": "2026-11-01T00:00:00Z",
    "base_currency": "USD",
    "benchmark": "spx",
}


def context(port: Any) -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="tenant:one", principal="p"),
        services={"finance_read_port": port},
    )


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def send(_c: Any, method: str, params: dict[str, Any], **_k: Any) -> Any:
        calls.append((method, params))
        return {
            "method": "twr",
            "currency": "USD",
            "value": None,
            "unavailable_reason": "no priced history",
            "benchmark": {"name": "spx", "value": "0.1050"},
        }

    monkeypatch.setattr(detail, "engine_send", send)
    monkeypatch.setattr(detail, "guard", lambda *_a, **_k: None)
    return calls


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.1")
async def test_card_is_verbatim_unavailable_never_zero_and_refuses_unverified_binding(
    sent: list[tuple[str, dict[str, Any]]],
) -> None:
    card = await detail.handle_market_performance(
        context(FakeReadPort()), PARAMS, NO_OP
    )
    # A missing figure stays null with its reason; Arbora never turns it into zero.
    assert card["value"]["value"] is None
    assert card["value"]["unavailable_reason"] == "no priced history"
    assert card["value"]["benchmark"] == {"name": "spx", "value": "0.1050"}
    assert sent[0][0] == "FinancePerformance"
    assert sent[0][1]["account"] == "acct-1" and sent[0][1]["benchmark"] == "spx"
    sent.clear()
    with pytest.raises(EngineRefusal) as refused:
        await detail.handle_market_performance(
            context(FakeReadPort(bindings={})), PARAMS, NO_OP
        )
    assert refused.value.code == "ACCOUNT_BINDING_UNVERIFIED"  # the engine vocabulary
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R009.1")
async def test_the_pinned_release_refuses_unavailable_naming_the_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    ids = generated.METHOD_IDS
    lacking = (
        {k: v for k, v in ids.items() if k != "FinancePerformance"}
        if isinstance(ids, dict)
        else frozenset(ids) - {"FinancePerformance"}
    )
    monkeypatch.setattr(generated, "METHOD_IDS", lacking)
    with pytest.raises(OperationRefused) as refused:
        await detail.handle_market_performance(context(FakeReadPort()), PARAMS, NO_OP)
    assert refused.value.code == "UNAVAILABLE"
    assert "FinancePerformance" in str(refused.value.details)
