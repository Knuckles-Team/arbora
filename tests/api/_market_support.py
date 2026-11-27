"""Shared fixtures for the served market tests (ARBORA-DATA-MARKET).

One recording engine answers the finance methods under a verified person, so a
test sees exactly what Arbora forwarded and what refusal it returned.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import (
    CREDENTIAL_ONE,
    McpAsk,
    RecordingEngine,
    capture_invocation,
    person,
)

TIMEFRAME = {"unit": "day"}
SERIES = {
    "calendar_id": "cal",
    "listing_id": "lst-1",
    "price_basis": "raw",
    "timeframe": TIMEFRAME,
}
REPLAY = {
    "records": [],
    "series": SERIES,
    "spec": {"kind": {"kind": "sma", "period": 3}, "version": 1},
}
SCAN = {"limit": 1, "states": []}
FLIP = {
    "alpha_permille": 50,
    "asset_class": "equity",
    "data_status": "valid",
    "direction": "bullish",
    "features": {"above_200w_sma": True, "regime": 1, "timeframe_agreement": True},
    "history": [],
    "horizon_bars": 5,
    "indicator_version": "1",
    "n_min": 3,
    "timeframe": TIMEFRAME,
}
BACKTEST = {
    "costs": {"fee_bps": 1, "slippage_bps": 1},
    "data_revisions": [],
    "fill_rule": "next_bar_open",
    "fills": [],
    "returns": [0.1],
    "signal_keys": [],
    "strategy": "demo",
    "universe": [],
    "validation": {
        "embargo": 0,
        "n_groups": 4,
        "n_test_groups": 2,
        "n_trials": 1,
        "performance": [[0.1, 0.2]],
        "purge_window": 0,
    },
}
SIZING_POLICY = {
    "fixed_point_scale": 1000000,
    "instrument_id": "lst-1",
    "policy_id": "p",
    "revision": "1",
    "risk_cap": 100,
    "target_weight_by_state": {"bull": 50},
    "unit": "bps",
}
SIZING_REQUEST = {
    "as_of": "2026-11-01T00:00:00Z",
    "horizon_unit": "day",
    "horizons": [1],
    "model_id": "m",
    "model_revision": "1",
    "source_revision": "1",
    "source_series_id": "s",
}


def plain(value: Any) -> Any:
    """Frozen wire params as plain dicts and lists, for comparison."""
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(item) for item in value]
    return value


class MarketServed:
    """The served HTTP app and MCP surface of one verified person over a recording engine."""

    def __init__(
        self,
        captured: dict[str, Any],
        served: Any,
        monkeypatch: pytest.MonkeyPatch,
        answers: dict[str, Any],
        scopes: tuple[str, ...] = ("finance:read",),
    ) -> None:
        self.app, client = served
        self.monkeypatch = monkeypatch
        self.captured = captured
        self.scopes = scopes
        self.engine = RecordingEngine().route(
            client,
            monkeypatch,
            answers,
            {CREDENTIAL_ONE: person("one", extra_scopes=scopes)},
        )

    def http(self, op_id: str, params: dict[str, Any], **headers: str) -> Any:
        from tests.test_served_operation_api import _call

        return _call(self.app, op_id, params, **headers)

    def mcp(
        self, op_id: str, params: dict[str, Any], verb: str = "ask", **extra: Any
    ) -> Any:
        ask = McpAsk(
            self.captured["invocation"],
            self.monkeypatch,
            person("one", extra_scopes=self.scopes),
        )
        return ask(verb, op_id, params, **extra)


def capture_with_bindings(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, Any]
) -> dict[str, Any]:
    """Capture the served invocation with ``extra`` composed into its host bindings."""
    import tests.test_served_operation_api as harness

    captured: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def capturing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {**(kwargs.get("bindings") or {}), **extra}
        captured["invocation"] = compose(**kwargs)
        return captured["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", capturing)
    return captured


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return capture_invocation(monkeypatch)
