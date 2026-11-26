"""Market positions, performance and compute served over HTTP and MCP (ARBORA-DATA-MARKET)."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
from medulla import EngineResponseError

from arbora.finance.markets import (
    project_account,
    resolve_finance_authority,
    ticks_to_decimal,
)
from tests.api._served_support import (
    McpAsk,
    capture_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, served  # noqa: F401


def _value(response: Any) -> Any:
    assert response.status_code == 200, response.text
    return response.json()["result"]


ONE = {"Authorization": "Bearer one"}
POSITIONS = {
    "account": "acct-1",
    "as_of": "2026-11-01T00:00:00Z",
    "known_at": "2026-11-02T00:00:00Z",
    "basis_policy": "fifo",
    "base_currency": "USD",
}


class FinanceEngine:
    """A recording engine with the finance methods of the served market operations."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def project(self, params: dict[str, Any], who: str) -> Any:
        self.calls.append(("FinanceAccountProject", params))
        if params["account"] == "other":
            raise EngineResponseError("ACCESS_DENIED", "ACCOUNT_BINDING_UNVERIFIED")
        return {
            "account": params["account"],
            "as_of": params["as_of"],
            "known_at": params["known_at"],
            "basis_policy": params["basis_policy"],
            "base_currency": params["base_currency"],
            "scale": 100_000_000,
            "input_digest": "d1",
            "figures": [
                {
                    "position": "AAA",
                    "name": "open_quantity",
                    "value_ticks": 1_250_000_000,
                    "source": "broker",
                    "as_of": "2026-11-01T00:00:00Z",
                    "session": "regular",
                },
                {
                    "position": "AAA",
                    "name": "realized_gain",
                    "value_ticks": -5_000_001,
                    "source": "broker",
                    "as_of": "2026-11-01T00:00:00Z",
                    "session": "regular",
                },
            ],
        }

    def performance(self, params: dict[str, Any], who: str) -> Any:
        self.calls.append(("FinancePerformance", params))
        return {
            "account": params["account"],
            "method": params["method"],
            "currency": params["base_currency"],
            "range": params["range"],
            "value_ticks": None,
            "value": None,
            "reason": "NO_VALUATION",
            "benchmark": {
                "name": "index",
                "value_ticks": 5,
                "value": "0.00000005",
                "reason": None,
            },
            "source": "broker",
            "session": "regular",
            "stale": True,
            "as_of": params["as_of"],
        }

    def size(self, params: dict[str, Any], who: str) -> Any:
        self.calls.append(("FinancePositionSize", params))
        if params["accuracy"] > 1:
            raise EngineResponseError("INVALID_ARGUMENT", "SIZING_INPUT_INVALID: x")
        return 0.01

    def shift(self, params: dict[str, Any], who: str) -> Any:
        self.calls.append(("FinanceDistributionShift", params))
        return {"statistic": 0.5, "shifted": True}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


def finance_person(name: str) -> Any:
    actor = person(name)
    return dataclasses.replace(actor, roles=(*actor.roles, "finance:read"))


@pytest.fixture
def env(stack: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    holder = stack
    app, client = served
    engine = FinanceEngine()
    route_methods(
        client,
        {
            "FinanceAccountProject": engine.project,
            "FinancePerformance": engine.performance,
            "FinancePositionSize": engine.size,
            "FinanceDistributionShift": engine.shift,
        },
        monkeypatch,
        {"one": finance_person("one")},
    )
    return app, engine, holder, monkeypatch


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.11")
def test_positions_carry_provenance(env: Any) -> None:
    app, engine, holder, monkeypatch = env
    over_http = _value(_call(app, "market.positions", POSITIONS, **ONE))
    figure = over_http["figures"][0]
    assert figure["value"] == "12.50000000" and figure["value_ticks"] == 1_250_000_000
    assert (figure["source"], figure["as_of"], figure["session"]) == (
        "broker",
        "2026-11-01T00:00:00Z",
        "regular",
    )
    assert over_http["figures"][1]["value"] == "-0.05000001"
    over_mcp = McpAsk(holder["invocation"], monkeypatch, finance_person("one"))(
        "ask", "market.positions", POSITIONS
    )
    assert over_mcp["result"] == over_http
    assert [name for name, _ in engine.calls] == ["FinanceAccountProject"] * 2


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.5")
def test_a_named_identity_is_refused_before_any_engine_call(env: Any) -> None:
    app, engine, *_ = env
    for field in ("tenant", "principal", "owner"):
        refused = _call(app, "market.positions", {**POSITIONS, field: "x"}, **ONE)
        assert refused.status_code == 400
    assert engine.calls == []


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.5")
def test_an_engine_binding_refusal_passes_through(env: Any) -> None:
    app, *_ = env
    refused = _call(app, "market.positions", {**POSITIONS, "account": "other"}, **ONE)
    assert refused.json()["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.5")
def test_authority_comes_from_the_verified_caller_only() -> None:
    from types import SimpleNamespace

    from arbora.api.invoke.pipeline import OperationRefused

    caller = SimpleNamespace(tenant="tenant:one", principal="person:one")
    authority = resolve_finance_authority(caller, {"account": "a"})
    assert (authority.tenant_id, authority.principal) == ("tenant:one", "person:one")
    with pytest.raises(OperationRefused) as named:
        resolve_finance_authority(caller, {"account": "a", "tenant_id": "x"})
    assert named.value.code == "INVALID_ARGUMENT"
    with pytest.raises(OperationRefused) as absent:
        resolve_finance_authority(SimpleNamespace(tenant="", principal="p"), {})
    assert absent.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.11")
def test_a_figure_without_provenance_is_never_served() -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    projection = FinanceEngine().project(POSITIONS, "")
    projection["figures"][0]["session"] = ""
    with pytest.raises(OperationRefused):
        project_account(projection)


def test_ticks_format_exactly() -> None:
    assert ticks_to_decimal(1, 100_000_000) == "0.00000001"
    assert ticks_to_decimal(-100_000_000, 100_000_000) == "-1.00000000"
    assert ticks_to_decimal(7, 1) == "7"
    with pytest.raises(ValueError):
        ticks_to_decimal(1, 3)


@pytest.mark.spec("ARBORA-DATA-MARKET-R009.1")
def test_card_is_verbatim_unavailable_never_zero(env: Any) -> None:
    app, engine, *_ = env
    card = _value(
        _call(
            app,
            "market.performance",
            {"account": "acct-1", "range": "1Y", "base_currency": "USD"},
            **ONE,
        )
    )
    assert card["value"] is None and card["unavailable_reason"] == "NO_VALUATION"
    assert card["benchmark"] == {
        "name": "index",
        "value": "0.00000005",
        "unavailable_reason": None,
    }
    assert card["stale"] is True
    sent = engine.calls[0][1]
    assert sent["method"] == "twr" and sent["benchmark"] is None
    assert (
        _call(
            app,
            "market.performance",
            {"account": "a", "range": "2Y", "base_currency": "USD"},
            **ONE,
        ).status_code
        == 400
    )


@pytest.mark.spec("ARBORA-DATA-MARKET-R003.13")
def test_sizing_and_shift_over_http_and_mcp(env: Any) -> None:
    app, engine, holder, monkeypatch = env
    sized = _value(
        _call(
            app,
            "market.position_size",
            {"accuracy": 0.6, "win_loss_ratio": 1.5},
            **ONE,
        )
    )
    assert sized == {"value": 0.01}
    # Omitted optionals stay omitted, so the engine applies its own knobs.
    assert engine.calls[-1][1] == {"accuracy": 0.6, "win_loss_ratio": 1.5}
    ask = McpAsk(holder["invocation"], monkeypatch, finance_person("one"))
    shifted = ask(
        "ask",
        "market.distribution_shift",
        {"reference": [0.1, 0.2], "current": [0.5, 0.6], "threshold": 0.3},
    )
    assert shifted["result"]["value"]["shifted"] is True
    refused = _call(
        app, "market.position_size", {"accuracy": 2, "win_loss_ratio": 1}, **ONE
    )
    assert refused.json()["error"]["code"] == "INVALID_ARGUMENT"
    assert (
        _call(
            app,
            "market.distribution_shift",
            {"reference": [], "current": [1.0], "threshold": 0.1},
            **ONE,
        ).status_code
        == 400
    )
