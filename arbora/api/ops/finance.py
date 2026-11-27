"""Position and paper-trading operations with subject ownership (ARBORA-OPS-R017.2).

Each operation runs under the service executor: ``prepare_executor`` resolves
the ``account_id`` parameter as the subject and asks the runtime whether the
caller owns it before any handler runs. A caller who does not own the account
gets ``SUBJECT_ACCESS_DENIED``; a request that names an owner is refused
``INVALID_ARGUMENT``. The handlers reach the venue only through the governed
fleet call, so a denied check makes zero provider calls.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, field_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops._nested import invoke_registered
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)
from arbora.finance import guides, read_models
from arbora.finance.fleet_read_port import ReadRequest

#: The venue connector server and its tools, reached through ``fleet.call``.
VENUE_SERVER = "emerald-exchange"
ORDERS_TOOL = "emerald_orders"
PORTFOLIO_TOOL = "emerald_portfolio"
READ_SCOPE = "finance:read"
PAPER_SCOPE = "finance:propose-order"
#: Replay owner of the paper order: the effect journal settles it once per request.
EXTERNAL_EFFECT = ("orders.paper_submit",)
_ID = Field(min_length=1, max_length=128)


class PositionsListParams(Params):
    account_id: str = _ID


class PaperSubmitParams(Params):
    account_id: str = _ID
    symbol: str = Field(min_length=1, max_length=64)
    side: str = Field(pattern="^(buy|sell)$")
    qty: float = Field(gt=0)
    order_type: str = Field(default="market", pattern="^(market|limit)$")
    limit_price: float = Field(default=0.0, ge=0)
    mode: str = Field(default="paper", max_length=16)


_RESOURCE_ID = Field(pattern=read_models.ID_PATTERN)
_INSTANT = Field(default=None, pattern=read_models.INSTANT_PATTERN)
_STATUS = Field(default=None, pattern=r"^[A-Za-z][A-Za-z0-9._-]{0,31}$")


class _PageParams(Params):
    """List and search: a bounded page and an opaque cursor, under a fixed server sort."""

    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = None

    @field_validator("cursor")
    @classmethod
    def _bounded_cursor(cls, value: str | None) -> str | None:
        if (
            value is not None
            and not 1 <= len(value.encode()) <= read_models.MAX_CURSOR_BYTES
        ):
            raise ValueError("cursor outside 1 to 512 bytes")
        return value


class _SearchParams(Params):
    query: str

    @field_validator("query")
    @classmethod
    def _nfc_query(cls, value: str) -> str:
        if unicodedata.normalize("NFC", value) != value:
            raise ValueError("query must be NFC")
        if not 1 <= len(value.encode()) <= 200:
            raise ValueError("query outside 1 to 200 bytes")
        return value


class _MarketFilters(Params):
    event_id: str | None = Field(default=None, pattern=read_models.ID_PATTERN)
    status: str | None = _STATUS
    starts_at_from: str | None = _INSTANT
    starts_at_to: str | None = _INSTANT
    ends_at_from: str | None = _INSTANT
    ends_at_to: str | None = _INSTANT


class _EventFilters(Params):
    status: str | None = _STATUS
    starts_at_from: str | None = _INSTANT
    starts_at_to: str | None = _INSTANT
    ends_at_from: str | None = _INSTANT
    ends_at_to: str | None = _INSTANT


class _OrderFilters(Params):
    market_id: str | None = Field(default=None, pattern=read_models.ID_PATTERN)
    status: str | None = _STATUS
    side: Literal["buy", "sell"] | None = None
    created_at_from: str | None = _INSTANT
    created_at_to: str | None = _INSTANT


class MarketsListParams(_PageParams, _MarketFilters):
    pass


class MarketsSearchParams(_SearchParams, _PageParams, _MarketFilters):
    pass


class MarketsGetParams(Params):
    market_id: str = _RESOURCE_ID


class EventsListParams(_PageParams, _EventFilters):
    pass


class EventsSearchParams(_SearchParams, _PageParams, _EventFilters):
    pass


class EventsGetParams(Params):
    event_id: str = _RESOURCE_ID


class OrdersListParams(_PageParams, _OrderFilters):
    pass


class OrdersSearchParams(_SearchParams, _PageParams, _OrderFilters):
    pass


class OrdersGetParams(Params):
    order_id: str = _RESOURCE_ID


class FinanceReply(Params):
    value: dict[str, Any]


async def _venue(context: Any, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    reply = await invoke_registered(
        context,
        "fleet.call",
        {"server": VENUE_SERVER, "tool": tool, "arguments": arguments},
    )
    value = reply.get("value", reply) if isinstance(reply, Mapping) else reply
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if isinstance(value, list):
        value = {"items": value}
    if not isinstance(value, Mapping) or set(value) == {"error"}:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": f"the venue did not answer {tool}"}
        )
    return dict(value)


async def handle_positions_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The open positions of one owned account."""
    answer = await _venue(context, PORTFOLIO_TOOL, {"action": "positions"})
    return {"value": {"account_id": params["account_id"], **answer}}


async def handle_orders_paper_submit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Submit one paper order for an owned account; a live mode is refused."""
    if params.get("mode", "paper") != "paper":
        raise OperationRefused("POLICY_DENIED", {"reason": "paper mode only"})
    arguments = {
        "action": "submit",
        "symbol": params["symbol"],
        "side": params["side"],
        "qty": params["qty"],
        "order_type": params.get("order_type", "market"),
        "limit_price": params.get("limit_price", 0.0),
    }
    answer = await _venue(context, ORDERS_TOOL, arguments)
    return {"value": {"account_id": params["account_id"], **answer}}


#: Per resource kind: the filters it accepts and the projection of its page and record.
_READS: Mapping[str, Any] = {
    "market": (
        (
            "event_id",
            "status",
            "starts_at_from",
            "starts_at_to",
            "ends_at_from",
            "ends_at_to",
        ),
        read_models.project_market_page,
        read_models.project_market,
        "market_id",
    ),
    "event": (
        ("status", "starts_at_from", "starts_at_to", "ends_at_from", "ends_at_to"),
        read_models.project_event_page,
        read_models.project_event,
        "event_id",
    ),
    "order": (
        ("market_id", "status", "side", "created_at_from", "created_at_to"),
        read_models.project_order_page,
        read_models.project_order,
        "order_id",
    ),
}


async def _authority(context: Any) -> tuple[Any, str, str]:
    """The read port, tenant and verified account, resolved before any provider read.

    Tenant and account come from verified authority and the port's binding
    authority, never from a request field. An unbound port or an unverified
    binding refuses ``UNAVAILABLE`` and has made no provider read.
    """
    tenant = getattr(context.caller, "tenant", None)
    if not isinstance(tenant, str) or not tenant:
        raise OperationRefused("UNAVAILABLE", {"reason": "finance authority absent"})
    port = bound_service(
        context, "finance_read_port", reason="the finance read port is not composed"
    )
    account = await port.account_binding(context, tenant)
    if not isinstance(account, str) or not account:
        raise OperationRefused("UNAVAILABLE", {"reason": "account binding unverified"})
    return port, tenant, account


async def _page(
    context: Any, params: Mapping[str, Any], kind: str, mode: str, op_id: str
) -> dict[str, Any]:
    filter_keys, project_page, _project, _id = _READS[kind]
    port, tenant, account = await _authority(context)
    filters = {key: params[key] for key in filter_keys if params.get(key) is not None}
    position = snapshot = None
    if params.get("cursor") is not None:
        position, snapshot = read_models.decode_cursor(
            params["cursor"], op=op_id, tenant=tenant, account=account, filters=filters
        )
    request = ReadRequest(
        tenant=tenant,
        account=account,
        limit=params["limit"],
        position=position,
        snapshot=snapshot,
        filters=filters,
        query=params.get("query") if mode == "search" else None,
    )
    answer = await getattr(port, f"{mode}_{kind}s")(context, request)
    onward = answer.get("next_cursor") if isinstance(answer, Mapping) else None
    cursor = None
    if isinstance(onward, str):
        cursor = read_models.encode_cursor(
            op=op_id,
            tenant=tenant,
            account=account,
            filters=filters,
            position=onward,
            snapshot=str(answer.get("snapshot") or ""),
        )
    return project_page(answer, limit=params["limit"], cursor=cursor)


async def _one(context: Any, params: Mapping[str, Any], kind: str) -> dict[str, Any]:
    _keys, _page_projection, project, id_key = _READS[kind]
    port, tenant, account = await _authority(context)
    request = ReadRequest(tenant=tenant, account=account, resource_id=params[id_key])
    return project(await getattr(port, f"get_{kind}")(context, request))


async def handle_markets_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _page(context, params, "market", "list", "markets.list")


async def handle_markets_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _page(context, params, "market", "search", "markets.search")


async def handle_markets_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _one(context, params, "market")


async def handle_events_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _page(context, params, "event", "list", "events.list")


async def handle_events_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _page(context, params, "event", "search", "events.search")


async def handle_events_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _one(context, params, "event")


async def handle_orders_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _page(context, params, "order", "list", "orders.list")


async def handle_orders_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _page(context, params, "order", "search", "orders.search")


async def handle_orders_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _one(context, params, "order")


#: ``(op id, params model, result model, summary, example)``; the handler is ``handle_<op id>``.
_READ_OPS: tuple[tuple[str, type[Params], type[Any], str, str], ...] = (
    (
        "markets.list",
        MarketsListParams,
        read_models.MarketPage,
        "List markets",
        "list the open markets",
    ),
    (
        "markets.search",
        MarketsSearchParams,
        read_models.MarketPage,
        "Search markets by text",
        "search markets for a phrase",
    ),
    (
        "markets.get",
        MarketsGetParams,
        read_models.MarketGet,
        "Read one market by id",
        "show this market",
    ),
    (
        "events.list",
        EventsListParams,
        read_models.EventPage,
        "List events",
        "list the upcoming events",
    ),
    (
        "events.search",
        EventsSearchParams,
        read_models.EventPage,
        "Search events by text",
        "search events for a phrase",
    ),
    (
        "events.get",
        EventsGetParams,
        read_models.EventGet,
        "Read one event by id",
        "show this event",
    ),
    (
        "orders.list",
        OrdersListParams,
        read_models.OrderPage,
        "List orders",
        "list my orders",
    ),
    (
        "orders.search",
        OrdersSearchParams,
        read_models.OrderPage,
        "Search orders by text",
        "search my orders for a phrase",
    ),
    (
        "orders.get",
        OrdersGetParams,
        read_models.OrderGet,
        "Read one order by id",
        "show this order",
    ),
)


class GuideGetParams(Params):
    pass


async def handle_guide_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The finance guide's revision header and body digest, from the venue's skill resource."""
    resource = await invoke_registered(
        context,
        "fleet.resources.read",
        {"server": guides.GUIDE_SERVER, "uri": guides.GUIDE_URI},
    )
    return guides.project_guide(resource)


def read_operations() -> tuple[OpSpec, ...]:
    """The nine read-only marketplace operations under ``finance:read``."""
    return (
        *(
            build_read_op(
                op_id=op_id,
                summary=summary,
                examples=(example,),
                params=params,
                result=result,
                handler=f"arbora.api.ops.finance.handle_{op_id.replace('.', '_')}",
                scope=READ_SCOPE,
            )
            for op_id, params, result, summary, example in _READ_OPS
        ),
        build_read_op(
            op_id="finance.guide.get",
            summary="Read the finance guide revision, reviewer and source cards",
            examples=("which revision of the trading guide is in force",),
            params=GuideGetParams,
            result=guides.GuideProjection,
            handler="arbora.api.ops.finance.handle_guide_get",
            scope=READ_SCOPE,
        ),
    )


def _service(op_id: str, **fields: Any) -> OpSpec:
    return OpSpec(
        id=op_id,
        binding=Composite(
            handler=f"arbora.api.ops.finance.handle_{op_id.replace('.', '_')}"
        ),
        executor=Executor.SERVICE,
        principals=PrincipalRule.HUMAN_UNDELEGATED,
        subject=SubjectRef(source=SubjectSource.PARAM, path="params.account_id"),
        result=FinanceReply,
        **fields,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        *read_operations(),
        _service(
            "positions.list",
            verb=Verb.ASK,
            summary="List the open positions of an account the caller owns",
            examples=("show my open positions",),
            params=PositionsListParams,
            scopes=frozenset({READ_SCOPE}),
            executor_scopes=frozenset({READ_SCOPE}),
            effect=Effect.READ,
        ),
        _service(
            "orders.paper_submit",
            verb=Verb.ACT,
            summary="Submit a paper order for an account the caller owns",
            examples=("paper trade this symbol",),
            params=PaperSubmitParams,
            scopes=frozenset({READ_SCOPE, PAPER_SCOPE}),
            executor_scopes=frozenset({PAPER_SCOPE}),
            effect=Effect.WRITE,
            confirm=Confirm.PLAN,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations

__all__ = [
    "EXTERNAL_EFFECT",
    "handle_orders_paper_submit",
    "handle_positions_list",
    "operations",
    "specs",
]
