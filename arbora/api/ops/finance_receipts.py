"""Finance delivery receipts read (ARBORA-DATA-MARKET-R009.4).

``finance.receipts.list`` forwards the engine ``FinanceReceiptList`` method for
the caller's tenant. The engine owns the receipt, its delivery machine and the
order of the list; Arbora keeps no receipt store and writes nothing. A receipt
id is a hash of the tenant, kind and idempotency key, so one receipt is one
item and a channel retry raises ``attempt`` without adding an item.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import OpSpec

ReceiptKind = Literal["alert_delivery", "schedule_run", "dca_proposal"]
ReceiptState = Literal["due", "claimed", "delivered", "completed", "missed", "failed"]


class ReceiptsListParams(Params):
    kind: ReceiptKind | None = None
    state: ReceiptState | None = None
    limit: int = Field(default=50, ge=1, le=200)


class ReceiptItem(Params):
    receipt_id: str
    event_id: str
    kind: str
    state: ReceiptState
    mode: str
    occurred_at: str
    channel: str
    attempt: int = Field(ge=1)


class ReceiptsListResult(Params):
    items: list[ReceiptItem]


def _item(view: Any) -> dict[str, Any]:
    try:
        return {
            "receipt_id": view["receipt_id"],
            "event_id": view["event_id"],
            "kind": view["receipt_kind"],
            "state": view["receipt_state"],
            "mode": view["mode"],
            "occurred_at": view["recorded_at"],
            "channel": view["channel"],
            "attempt": view["attempt"],
        }
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


async def handle_receipts_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The caller tenant's receipts, as the engine orders and limits them."""
    request: dict[str, Any] = {
        "tenant_key": str(context.caller.tenant),
        "limit": params["limit"],
    }
    for key in ("kind", "state"):
        if params.get(key) is not None:
            request[key] = params[key]
    page = await engine_send(context, "FinanceReceiptList", request)
    rows = page.get("receipts") if isinstance(page, Mapping) else None
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    return {"items": [_item(row) for row in rows]}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="finance.receipts.list",
            summary="List DCA, alert and schedule delivery receipts with their state",
            examples=("show the delivery state of my finance alerts",),
            params=ReceiptsListParams,
            result=ReceiptsListResult,
            handler="arbora.api.ops.finance_receipts.handle_receipts_list",
            scope="finance:read",
        ),
    )


specs = operations

__all__ = [
    "ReceiptsListParams",
    "ReceiptsListResult",
    "handle_receipts_list",
    "operations",
    "specs",
]
