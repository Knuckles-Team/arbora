"""Served market operations over the engine's finance methods (ARBORA-DATA-MARKET).

``market.positions`` and ``market.performance`` read one account's engine
projection under the verified caller; ``market.position_size`` and
``market.distribution_shift`` send one engine calculation through the market
compute port. Every figure is the engine's. Arbora resolves the authority
first, forwards omitted optionals as omitted, and does no arithmetic.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec
from arbora.finance.markets import (
    project_account,
    project_performance,
    resolve_finance_authority,
)

READ_SCOPE = "finance:read"
_ACCOUNT = Field(min_length=1, max_length=256)
_INSTANT_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
_INSTANT = Field(pattern=_INSTANT_PATTERN)
_CURRENCY = Field(pattern=r"^[A-Z]{3}$")
_SAMPLE = Field(min_length=1, max_length=10_000)


class MarketPositionsParams(Params):
    account: str = _ACCOUNT
    as_of: str = _INSTANT
    known_at: str = _INSTANT
    basis_policy: Literal["fifo", "lifo", "average_cost"] = "fifo"
    base_currency: str = _CURRENCY


class MarketPerformanceParams(Params):
    account: str = _ACCOUNT
    range: Literal["1D", "1M", "YTD", "1Y", "5Y", "ALL"]
    base_currency: str = _CURRENCY
    method: Literal["twr", "mwr", "xirr"] = "twr"
    benchmark: str | None = Field(default=None, min_length=1, max_length=64)
    as_of: str | None = Field(default=None, pattern=_INSTANT_PATTERN)
    known_at: str | None = Field(default=None, pattern=_INSTANT_PATTERN)


class PositionSizeParams(Params):
    accuracy: float
    win_loss_ratio: float
    fraction: float | None = None
    max_risk: float | None = None
    series_revision: str | None = Field(default=None, min_length=1, max_length=256)


class DistributionShiftParams(Params):
    reference: list[float] = _SAMPLE
    current: list[float] = _SAMPLE
    threshold: float


class Figure(Params):
    position: str
    name: str
    value_ticks: int
    value: str
    source: str
    as_of: str
    session: str


class MarketPositionsResult(Params):
    account: str
    as_of: str
    known_at: str
    basis_policy: str
    base_currency: str
    scale: int
    input_digest: str
    figures: list[Figure]


class BenchmarkCard(Params):
    name: str
    value: str | None
    unavailable_reason: str | None


class MarketPerformanceResult(Params):
    method: str
    currency: str
    range: str
    value: str | None
    benchmark: BenchmarkCard | None
    source: str
    as_of: str
    session: str
    stale: bool
    unavailable_reason: str | None


class ComputeResult(Params):
    value: Any


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


async def handle_market_positions(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One account's lots, corporate actions and valuation, as the engine projects them."""
    resolve_finance_authority(context.caller, params)
    projection = await engine_send(context, "FinanceAccountProject", dict(params))
    if not isinstance(projection, Mapping):
        raise unavailable("engine result malformed")
    return project_account(projection)


async def handle_market_performance(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One account's return card over a range; a missing figure stays null."""
    resolve_finance_authority(context.caller, params)
    now = _now()
    request = {
        "account": params["account"],
        "as_of": params.get("as_of") or now,
        "known_at": params.get("known_at") or now,
        "base_currency": params["base_currency"],
        "method": params["method"],
        "range": params["range"],
        "benchmark": params.get("benchmark"),
    }
    card = await engine_send(context, "FinancePerformance", request)
    if not isinstance(card, Mapping):
        raise unavailable("engine result malformed")
    return project_performance(card)


async def _compute(
    context: Any, method: str, request: Mapping[str, Any]
) -> dict[str, Any]:
    authority = resolve_finance_authority(context.caller, request)
    port = context.services.get("market_compute_port")
    if port is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "market compute unavailable"})
    answer = await port.call(method, request, tenant=authority.tenant_id)
    return {"value": answer}


async def handle_position_size(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's fractional-Kelly stake; omitted optionals stay omitted."""
    request = {k: v for k, v in params.items() if v is not None}
    return await _compute(context, "FinancePositionSize", request)


async def handle_distribution_shift(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's two-sample shift test between a reference and a current sample."""
    return await _compute(context, "FinanceDistributionShift", dict(params))


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.market"
    return (
        build_read_op(
            op_id="market.positions",
            summary="Read one account's positions, cost basis and valuation with source and as-of",
            examples=("show my positions",),
            params=MarketPositionsParams,
            result=MarketPositionsResult,
            handler=f"{module}.handle_market_positions",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="market.performance",
            summary="Read one account's return over a range, with an optional benchmark",
            examples=("how has my account performed this year",),
            params=MarketPerformanceParams,
            result=MarketPerformanceResult,
            handler=f"{module}.handle_market_performance",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="market.position_size",
            summary="Size a position by fractional Kelly from accuracy and win-loss ratio",
            examples=("how large should this position be",),
            params=PositionSizeParams,
            result=ComputeResult,
            handler=f"{module}.handle_position_size",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="market.distribution_shift",
            summary="Test whether a current sample has shifted from a reference sample",
            examples=("has the return distribution shifted",),
            params=DistributionShiftParams,
            result=ComputeResult,
            handler=f"{module}.handle_distribution_shift",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_distribution_shift",
    "handle_market_performance",
    "handle_market_positions",
    "handle_position_size",
    "operations",
    "specs",
]
