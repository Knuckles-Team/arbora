"""Confirmed order, position and wallet changes and the order proposal door (ARBORA-DATA-MARKET-R007).

``orders.change``, ``positions.change`` and ``wallet.operate`` are ``Confirm.PLAN`` effect
operations that reuse the invoke pipeline's ``_effect`` and ``bind_plan``. Nothing reaches a
provider before the plan is confirmed and every durable binding exists: an absent port
refuses ``UNAVAILABLE``. A wallet operation also needs the SDK intent contract (guarded by
``arbora.pin_lag.scion_sdk``), the SDK proof verdict unchanged, an engine authorization and a
durable effect reservation; an uncertain prior effect enters reconciliation and is never
retried blind. ``market.order.stage`` and ``market.order.confirm`` call the engine proposal
door (``FinanceOrderStage`` and ``FinanceOrderPromote``); arbora supplies the gate receipts
and holds the human confirmation, the engine decides.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_write_op
from arbora.api.registry import Confirm, OpSpec
from arbora.finance.changes import (
    OrderChange,
    PositionChange,
    WalletOperation,
    refusal_for,
    safe_evidence,
)
from arbora.pin_lag.medulla import send_main
from arbora.pin_lag.unavailable import PinLagUnavailable

SCOPE = "finance:propose-order"
#: The engine replays a stage and a promotion by the proposal revision it holds; a change
#: effect is settled once by the effect journal.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "market.order.stage": "medulla:FinanceOrderStage",
    "market.order.confirm": "medulla:FinanceOrderPromote",
}
EXTERNAL_EFFECT = ("orders.change", "positions.change", "wallet.operate")
_DECIMAL = r"^-?\d+(\.\d+)?$"


class OrderChangeParams(Params):
    order_id: str = Field(min_length=1, max_length=128)
    action: str = Field(pattern="^(amend|cancel)$")
    account_id: str = Field(min_length=1, max_length=256)
    quantity: str | None = Field(default=None, pattern=_DECIMAL)
    limit_price: str | None = Field(default=None, pattern=_DECIMAL)


class PositionChangeParams(Params):
    position_id: str = Field(min_length=1, max_length=128)
    account_id: str = Field(min_length=1, max_length=256)
    target_quantity: str = Field(pattern=_DECIMAL)


class WalletOperateParams(Params):
    """``proof`` travels in the request envelope (``OpSpec.envelope``), not in the plan."""

    intent: dict[str, Any]
    confirmation: dict[str, Any] | None = None
    proof: dict[str, Any] | None = None


class StageParams(Params):
    snapshot: dict[str, Any]
    expected_head_revision: int | None = Field(default=None, ge=0)
    expected_head_digest: str | None = None


class ConfirmParams(Params):
    proposal_id: str = Field(min_length=1, max_length=256)
    candidate_revision: int = Field(ge=0)
    snapshot_digest: str = Field(min_length=1, max_length=128)
    required_gate_manifest_digest: str = Field(min_length=1, max_length=128)
    manifest_gate_ids: list[str] = Field(max_length=64)
    gate_receipts: list[dict[str, Any]] = Field(max_length=64)
    expected_promoted_revision: int | None = Field(default=None, ge=0)


class ChangeResult(Params):
    value: dict[str, Any]
    evidence: dict[str, str] | None = None


def _who(context: Any) -> tuple[str, str]:
    return str(context.caller.tenant), str(context.caller.principal)


def _refuse(code: str, details: Mapping[str, Any] | None = None) -> OperationRefused:
    return OperationRefused(code, dict(details or {}))


async def _change(context: Any, kind: str, model: Any) -> dict[str, Any]:
    port = bound_service(
        context, "finance_change_port", reason="the change door is not composed"
    )
    receipt = await port.apply(
        context, kind, model.identity(), model.model_dump(), context.idempotency_key
    )
    return {"value": {"identity": model.identity(), **dict(receipt)}}


async def handle_orders_change(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant, actor = _who(context)
    try:
        model = OrderChange(tenant_id=tenant, actor=actor, **dict(params))
    except ValidationError:
        raise _refuse("INVALID_ARGUMENT", {"field": "change"}) from None
    if model.action == "amend" and model.quantity is None and model.limit_price is None:
        raise _refuse("INVALID_ARGUMENT", {"field": "change"})
    return await _change(context, "order", model)


async def handle_positions_change(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant, actor = _who(context)
    try:
        model = PositionChange(tenant_id=tenant, actor=actor, **dict(params))
    except ValidationError:
        raise _refuse("INVALID_ARGUMENT", {"field": "change"}) from None
    return await _change(context, "position", model)


def _wallet(context: Any, params: Mapping[str, Any]) -> WalletOperation:
    tenant, actor = _who(context)
    try:
        return WalletOperation.from_params(tenant, actor, params["intent"])
    except PinLagUnavailable as missing:
        raise _refuse("UNAVAILABLE", {"missing": missing.missing}) from None
    except ValidationError:
        raise _refuse("INVALID_ARGUMENT", {"field": "intent"}) from None


def _confirmed(wallet: WalletOperation, params: Mapping[str, Any]) -> None:
    confirmation = params.get("confirmation")
    if not confirmation:
        raise _refuse("CONFIRMATION_REQUIRED")
    if confirmation.get("plan_digest") != wallet.plan_digest():
        raise _refuse("PLAN_MISMATCH")


def _effect_key(verdict: Mapping[str, Any], context: Any) -> str | None:
    return verdict.get("effect_key") or context.idempotency_key


async def handle_wallet_operate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    wallet = _wallet(context, params)
    _confirmed(wallet, params)
    ports = bound_service(
        context, "wallet_operations", reason="the wallet authority is not composed"
    )
    projection = wallet.projection()
    verdict = dict(await ports.verify(projection, context.envelope.get("proof")))
    if verdict.get("outcome") != "verified":
        code, details = refusal_for(str(verdict.get("reason", "")))
        raise _refuse(code, details)
    decision = dict(await ports.authorize(projection))
    if decision.get("allowed") is not True:
        raise _refuse("POLICY_DENIED", {"reason": "unentitled"})
    # The effect is keyed by what the proof signed, so a second plan carrying the same proof
    # finds the first reservation and never applies twice.
    key = _effect_key(verdict, context)
    receipt = dict(await ports.apply(projection, key))
    state = "reconciling" if receipt.get("state") == "uncertain" else "applied"
    if state == "reconciling":
        await ports.reconcile(projection, key)
    evidence = safe_evidence({**verdict, **decision, **receipt})
    return {
        "value": {"plan_digest": projection["plan_digest"], "state": state, **evidence},
        "evidence": evidence,
    }


async def handle_order_stage(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    body = {k: v for k, v in params.items() if v is not None}
    answer = await send_main(context, "FinanceOrderStage", body, idempotent=True)
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_order_confirm(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant, actor = _who(context)
    request = {k: v for k, v in params.items() if v is not None}
    request.update(tenant_id=tenant, approver=actor)
    answer = await send_main(
        context, "FinanceOrderPromote", {"request": request}, idempotent=True
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


def _op(
    op_id: str,
    summary: str,
    params: Any,
    handler: str,
    envelope: tuple[str, ...] = (),
) -> OpSpec:
    return build_write_op(
        envelope=envelope,
        op_id=op_id,
        summary=summary,
        examples=(summary.lower(),),
        params=params,
        result=ChangeResult,
        handler=f"arbora.api.ops.finance_changes.{handler}",
        scope=SCOPE,
        confirm=Confirm.PLAN,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _op("orders.change", "Amend or cancel an open order", OrderChangeParams, "handle_orders_change"),
        _op("positions.change", "Change a position to a target quantity", PositionChangeParams, "handle_positions_change"),
        _op("wallet.operate", "Run one confirmed wallet operation", WalletOperateParams, "handle_wallet_operate", ("proof",)),
        _op("market.order.stage", "Stage an order proposal with the engine", StageParams, "handle_order_stage"),
        _op("market.order.confirm", "Confirm and promote a staged order proposal", ConfirmParams, "handle_order_confirm"),
    )  # fmt: skip


specs = operations

__all__ = [
    "EXTERNAL_EFFECT",
    "NATIVE_IDEMPOTENCY",
    "handle_order_confirm",
    "handle_order_stage",
    "handle_orders_change",
    "handle_positions_change",
    "handle_wallet_operate",
    "operations",
    "specs",
]
