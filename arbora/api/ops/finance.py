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
from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params
from arbora.api.ops._nested import invoke_registered
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Executor,
    Idempotency,
    OpSpec,
    PrincipalRule,
    SubjectRef,
    SubjectSource,
    Verb,
)

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
