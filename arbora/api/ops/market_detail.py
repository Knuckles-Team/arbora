"""Instrument detail: rates and dividends (ARBORA-DATA-MARKET-R009.2).

``market.instrument.detail`` has two tabs. ``rates`` projects the listing's quoted
outcomes through the same read path as ``markets.get`` (R003.1): Arbora copies the
provider's name, price, currency, as-of and source and derives nothing. ``dividends``
sends the listing's corporate-action records to the engine ``FinanceMarket`` op
``corporate_actions`` (MEDULLA-FINANCE-PRIMITIVES-R006.1), which returns the
point-in-time view (``known_at <= as_of``, the latest revision per
``(listing_id, effective_time)``), and maps only what the engine answers. The engine
holds no pay date and no currency for a dividend, so the result carries neither.
The records come from the venue connector through the governed fleet call; the pinned
release lacks the op, so the tab refuses ``UNAVAILABLE`` naming it (PIN-LAG).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.domain.finance.markets import project_account, project_performance
from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import (
    Params,
    bound_service,
    build_read_op,
    build_write_op,
)
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.finance import _authority, _one
from arbora.api.ops.market_compute import READ_SCOPE, VENUE_SERVER, _tool_value
from arbora.api.ops.nested_invoke import invoke_registered
from arbora.api.ops.pin_guards import guard
from arbora.api.registry import OpSpec

#: The venue tool that answers one listing's corporate-action records in the engine shape.
ACTIONS_TOOL = "emerald_exchange_corporate_actions"
_LISTING = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
#: The engine request shape the dividends tab sends (engine ``main``, unreleased).
ENGINE_MODELS = ("FinanceMarketOpCorporateActions", "CorporateAction")


class InstrumentDetailParams(Params):
    listing_id: str = Field(pattern=_LISTING)
    tab: Literal["rates", "dividends"]
    as_of: int | None = Field(default=None, ge=0)


class InstrumentDetailResult(Params):
    value: dict[str, Any]


_STAMP = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
_CURRENCY = r"^[A-Z]{3}$"
_NUMBERS = Field(min_length=1, max_length=100_000)


class PositionsParams(Params):
    as_of: str = Field(pattern=_STAMP)
    known_at: str = Field(pattern=_STAMP)
    base_currency: str = Field(pattern=_CURRENCY)
    basis_policy: str = Field(min_length=1, max_length=64)


class PerformanceParams(Params):
    range: Literal["1D", "1M", "YTD", "1Y", "5Y", "ALL"]
    method: str = Field(min_length=1, max_length=64)
    as_of: str = Field(pattern=_STAMP)
    known_at: str = Field(pattern=_STAMP)
    base_currency: str = Field(pattern=_CURRENCY)
    benchmark: str | None = Field(default=None, min_length=1, max_length=128)


class PositionSizeParams(Params):
    accuracy: float
    win_loss_ratio: float
    fraction: float | None = None
    max_risk: float | None = None
    series_revision: str | None = Field(default=None, min_length=1, max_length=256)


class DistributionShiftParams(Params):
    current: list[float] = _NUMBERS
    reference: list[float] = _NUMBERS
    threshold: float


class ForecastSampleParams(Params):
    series_ids: list[str] = Field(min_length=1, max_length=256)
    model_revision: str = Field(min_length=1, max_length=256)
    temperature: float
    top_p: float
    sample_count: int
    seed: int = Field(ge=0)
    source_id: str = Field(min_length=1, max_length=256)


#: The venue tool the forecast provider adapter calls, through the governed fleet call.
FORECAST_TOOL = "emerald_forecast_sample"


async def handle_market_forecast_sample(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One forecast sample, stored as a probabilistic record; never a raw sample as fact."""
    from arbora.pin_lag.scion_sdk import PinLagUnavailable, require_forecast

    try:
        forecast = require_forecast()
    except PinLagUnavailable as missing:
        raise OperationRefused("UNAVAILABLE", {"missing": missing.missing}) from missing
    guard("SourceIngest")

    async def call(tool: str, arguments: dict[str, Any]) -> Any:
        return await invoke_registered(
            context,
            "fleet.call",
            {"server": VENUE_SERVER, "tool": tool, "arguments": arguments},
        )

    request = forecast.ForecastRequest(
        series_ids=tuple(params["series_ids"]),
        model_revision=params["model_revision"],
        temperature=params["temperature"],
        top_p=params["top_p"],
        sample_count=params["sample_count"],
        seed=params["seed"],
    )
    try:
        response = await forecast.request_forecast(
            forecast.forecast_provider(call, FORECAST_TOOL), request
        )
    except forecast.ForecastError as refused:
        # The provider codes of the SDK pass through unchanged.
        raise OperationRefused(str(refused.code.value)) from None
    body = forecast.forecast_ingest_request(
        response, tenant=str(context.caller.tenant), source_id=params["source_id"]
    )
    stored = await engine_send(context, "SourceIngest", body)
    digest = stored.get("record_digest") if isinstance(stored, Mapping) else None
    return {"value": {"record_digest": digest, "probabilistic": True}}


async def _verified(context: Any) -> tuple[str, Any]:
    """The verified account, resolved before any engine call (never a request field)."""
    try:
        _port, _tenant, account = await _authority(context)
    except OperationRefused as refused:
        if (refused.details or {}).get("reason") == "account binding unverified":
            raise EngineRefusal("ACCOUNT_BINDING_UNVERIFIED") from None
        raise

    async def send(method: str, params: dict[str, Any]) -> Any:
        return await engine_send(context, method, params)

    return account, send


async def handle_market_positions(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The account's positions, every figure verbatim with its source, as-of and session."""
    account, send = await _verified(context)
    return {"value": await project_account(send, account=account, **params)}


async def handle_market_performance(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The performance card as the engine produced it; a missing figure is never zero."""
    account, send = await _verified(context)
    guard("FinancePerformance")
    return {"value": await project_performance(send, account=account, **params)}


async def _compute(
    context: Any, method: str, model: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    from medulla.generated import models

    port = bound_service(
        context, "market_compute_port", reason="the market compute port is not composed"
    )
    body = {k: v for k, v in params.items() if v is not None}
    getattr(models, model).model_validate(body)
    return {"value": await port.call(context, method, body)}


async def handle_market_position_size(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _compute(
        context, "FinancePositionSize", "MethodFinancePositionSizeParams", params
    )


async def handle_market_distribution_shift(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _compute(
        context,
        "FinanceDistributionShift",
        "MethodFinanceDistributionShiftParams",
        params,
    )


def _not_found() -> OperationRefused:
    return OperationRefused("LISTING_NOT_FOUND")


async def _rates(context: Any, listing_id: str) -> dict[str, Any]:
    try:
        answer = await _one(context, {"market_id": listing_id}, "market")
    except OperationRefused as refused:
        # Absent and foreign answer alike: the listing is not found.
        if refused.code == "NOT_FOUND":
            raise _not_found() from None
        raise
    item = answer.get("item") if isinstance(answer, Mapping) else None
    if not isinstance(item, Mapping):
        raise _not_found()
    rates = [
        {
            "name": outcome["name"],
            "value": outcome["price"],
            "currency": outcome["currency"],
            "as_of": item["as_of"],
            "source": item["source"],
        }
        for outcome in item["outcomes"]
    ]
    return {"listing_id": listing_id, "rates": rates}


async def _records(context: Any, listing_id: str) -> list[dict[str, Any]]:
    service = (
        context.services.get("corporate_action_source") if context.services else None
    )
    if service is not None:
        raw: Any = await service.records(context, listing_id)
    else:
        reply = await invoke_registered(
            context,
            "fleet.call",
            {
                "server": VENUE_SERVER,
                "tool": ACTIONS_TOOL,
                "arguments": {"listing_id": listing_id},
            },
        )
        value = _tool_value(reply)
        raw = value.get("records") if isinstance(value, Mapping) else value
    if not isinstance(raw, list):
        raise OperationRefused("UNAVAILABLE", {"reason": "corporate actions malformed"})
    return [
        r for r in raw if isinstance(r, Mapping) and r.get("listing_id") == listing_id
    ]


def _dividend(row: Any) -> dict[str, Any] | None:
    """One engine ``CorporateAction`` as a dividend row; other kinds are not dividends."""
    if not isinstance(row, Mapping):
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    action = row.get("action")
    if not isinstance(action, Mapping) or action.get("kind") != "dividend":
        return None
    try:
        return {
            "ex_date": row["effective_time"],
            "amount_ticks": action["amount_ticks"],
            "source": row["source"],
            "revision": row["revision"],
            "known_at": row["known_at"],
        }
    except KeyError:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "engine result malformed"}
        ) from None


async def _dividends(
    context: Any, listing_id: str, as_of: int | None
) -> dict[str, Any]:
    guard("FinanceMarket", models=ENGINE_MODELS)
    op: dict[str, Any] = {
        "op": "corporate_actions",
        "records": await _records(context, listing_id),
    }
    if as_of is not None:
        op["as_of"] = as_of
    answer = await engine_send(context, "FinanceMarket", {"op": op})
    rows = answer.get("records") if isinstance(answer, Mapping) else answer
    if not isinstance(rows, list):
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    kept = [row for row in map(_dividend, rows) if row is not None]
    return {"listing_id": listing_id, "dividends": kept}


async def handle_market_instrument_detail(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    listing_id = params["listing_id"]
    if params["tab"] == "rates":
        return {"value": await _rates(context, listing_id)}
    return {"value": await _dividends(context, listing_id, params.get("as_of"))}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="market.instrument.detail",
            summary="Read an instrument's rates or its point-in-time dividends",
            examples=("show the dividends for this listing",),
            params=InstrumentDetailParams,
            result=InstrumentDetailResult,
            handler="arbora.api.ops.market_detail.handle_market_instrument_detail",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="market.forecast.sample",
            summary="Sample a forecast and store it as a probabilistic record",
            examples=("sample a forecast for these series",),
            params=ForecastSampleParams,
            result=InstrumentDetailResult,
            handler="arbora.api.ops.market_detail.handle_market_forecast_sample",
            scope="finance:track",
        ),
        build_read_op(
            op_id="market.positions",
            summary="Read the account's positions with source, as-of and session",
            examples=("show my positions",),
            params=PositionsParams,
            result=InstrumentDetailResult,
            handler="arbora.api.ops.market_detail.handle_market_positions",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="market.performance",
            summary="Read the account performance card against an optional benchmark",
            examples=("how is my portfolio performing this year",),
            params=PerformanceParams,
            result=InstrumentDetailResult,
            handler="arbora.api.ops.market_detail.handle_market_performance",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="market.position_size",
            summary="Size a position with the engine's sizing rule",
            examples=("how large should this position be",),
            params=PositionSizeParams,
            result=InstrumentDetailResult,
            handler="arbora.api.ops.market_detail.handle_market_position_size",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="market.distribution_shift",
            summary="Test whether a return series has shifted from its reference",
            examples=("has this distribution shifted",),
            params=DistributionShiftParams,
            result=InstrumentDetailResult,
            handler="arbora.api.ops.market_detail.handle_market_distribution_shift",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_market_forecast_sample",
    "handle_market_distribution_shift",
    "handle_market_instrument_detail",
    "handle_market_performance",
    "handle_market_position_size",
    "handle_market_positions",
    "operations",
    "specs",
]
