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

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send
from arbora.api.ops._nested import invoke_registered
from arbora.api.ops.finance import _one
from arbora.api.ops.market_compute import READ_SCOPE, VENUE_SERVER, _tool_value
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import guard

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
    )


specs = operations

__all__ = ["handle_market_instrument_detail", "operations", "specs"]
