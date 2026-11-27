"""Recurring DCA plan model and non-duplicating proposal key (ARBORA-DATA-MARKET-R006, FI-10).

``DCAPlan`` is the typed, durable record of a recurring dollar-cost-averaging
plan: owner, tenant, symbol, account binding, decimal amount and currency,
timezone, recurrence rule, bounds, pause state, plan revision, and paper or
live mode. ``build_dca_proposal_key`` derives one deterministic proposal key
per due occurrence from the plan's revision, so replay or clock drift can
never produce two proposals for the same occurrence. ``record_dca_proposal``
is the dedupe core, mirroring ``record_schedule_run``. Binding live-mode
proposals to human approval and the MERISTEM-CONTEXT-R006 connector write-back
lease, and reporting a missed contribution, is GDM-07a follow-on work; this
module owns only the plan model and the proposal dedupe record.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.finance.receipts import (
    FinanceReceiptEngine,
    claim_receipt,
    complete_receipt,
)

__all__ = [
    "DCAEvaluationError",
    "DCAHost",
    "DCAPlan",
    "DCAProposalOutcome",
    "PlanMode",
    "build_dca_proposal_key",
    "evaluate_dca",
    "minor_units",
    "record_dca_proposal",
    "run_dca_due",
]

PlanMode = Literal["paper", "live"]
DCAProposalOutcome = Literal["proposed", "duplicate"]


class DCAPlan(BaseModel):
    """One recurring dollar-cost-averaging plan."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=0)
    owner: str = Field(min_length=1, max_length=256)
    tenant_id: str = Field(min_length=1, max_length=256)
    symbol: str = Field(min_length=1, max_length=32)
    account_id: str = Field(min_length=1, max_length=256)
    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    timezone: str = Field(min_length=1, max_length=64, description="IANA zone.")
    recurrence_rule: str = Field(min_length=1, max_length=256)
    paused: bool = False
    mode: PlanMode = "paper"


def build_dca_proposal_key(plan: DCAPlan, occurrence: str) -> str:
    """Derive one deterministic proposal key for a due occurrence.

    The key is derived from the plan's revision, not wall-clock time, so a
    revised plan's earlier proposals never collide with its later ones, and
    replaying the same occurrence under the same revision always derives the
    same key.
    """

    return f"{plan.plan_id}:{plan.revision}:{occurrence}"


async def record_dca_proposal(
    engine: FinanceReceiptEngine, plan: DCAPlan, occurrence: str
) -> DCAProposalOutcome:
    """Propose one DCA occurrence exactly once per plan revision.

    FI-10: replaying the same ``occurrence`` under the same plan revision --
    whether from clock drift or a retried trigger -- returns ``"proposed"``
    once and ``"duplicate"`` on every subsequent call.
    """

    key = build_dca_proposal_key(plan, occurrence)
    claimed = await claim_receipt(
        engine,
        plan.tenant_id,
        "dca_proposal",
        key,
        event_id=key,
        mode=plan.mode,
        recorded_at=datetime.now(UTC).isoformat(),
    )
    if not claimed:
        return "duplicate"
    await complete_receipt(engine, plan.tenant_id, "dca_proposal", key)
    return "proposed"


#: ISO 4217 minor-unit exponents other than two (a unit conversion, not a formula).
_ZERO_DECIMAL = frozenset({"JPY", "KRW", "VND", "CLP", "ISK", "UGX", "XAF", "XOF"})
_THREE_DECIMAL = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})
DCA_STRATEGY_VERSION = 1

Send = Callable[[str, dict[str, Any]], Awaitable[Any]]


class DCAEvaluationError(ValueError):
    """The plan cannot be sized: a non-integral minor-unit amount or a bad engine answer."""

    code = "INVALID_ARGUMENT"


def minor_units(amount: Decimal, currency: str) -> int:
    """The amount in the currency's minor units; a fractional minor unit is refused."""
    exponent = (
        0 if currency in _ZERO_DECIMAL else 3 if currency in _THREE_DECIMAL else 2
    )
    scaled = amount.scaleb(exponent)
    if scaled != scaled.to_integral_value():
        raise DCAEvaluationError("amount is not a whole number of minor units")
    return int(scaled)


async def evaluate_dca(send: Send, plan: DCAPlan) -> Mapping[str, Any]:
    """Size one proposal with the engine's DCA evaluator (``strategy_evaluate``).

    The sizing is the engine's (MEDULLA-FINANCE-PRIMITIVES-R007.1); Arbora holds no
    formula and only converts the decimal amount to the minor units the op takes. The
    answer is ``{strategy_version, side, amount_minor_units}``.
    """
    spec = {
        "version": DCA_STRATEGY_VERSION,
        "kind": {
            "DollarCostAverageFixedAmount": {
                "amount_minor_units": minor_units(plan.amount, plan.currency)
            }
        },
    }
    answer = await send(
        "FinanceMarket", {"op": {"op": "strategy_evaluate", "spec": spec}}
    )
    if not isinstance(answer, Mapping) or "amount_minor_units" not in answer:
        raise DCAEvaluationError("engine strategy answer malformed")
    return answer


class DCAHost:
    """What a due occurrence needs beyond the engine: the paper door and the event."""

    async def submit_paper(self, plan: DCAPlan, proposal: Mapping[str, Any]) -> None:
        raise NotImplementedError

    async def missed(self, plan: DCAPlan, occurrence: str, reason: str) -> None:
        raise NotImplementedError


async def run_dca_due(
    engine: FinanceReceiptEngine,
    send: Send,
    host: DCAHost,
    plan: DCAPlan,
    occurrence: str,
) -> DCAProposalOutcome | Literal["paused", "missed"]:
    """One due occurrence: claim its key, size it by the engine, then paper-fill or hold.

    A paused plan does nothing. A replay finds the claimed key and proposes nothing. A
    failed sizing or paper fill yields the operator event ``DCA_MISSED``; the occurrence is
    never skipped silently and never doubled. Live mode stays a proposal for a human.
    """
    if plan.paused:
        return "paused"
    key = build_dca_proposal_key(plan, occurrence)
    claimed = await claim_receipt(
        engine,
        plan.tenant_id,
        "dca_proposal",
        key,
        event_id=key,
        mode=plan.mode,
        recorded_at=datetime.now(UTC).isoformat(),
    )
    if not claimed:
        return "duplicate"
    try:
        proposal = await evaluate_dca(send, plan)
        if plan.mode == "paper":
            await host.submit_paper(plan, proposal)
    except Exception as error:  # noqa: BLE001 - every failure is reported as a missed occurrence
        await host.missed(plan, occurrence, type(error).__name__)
        return "missed"
    await complete_receipt(engine, plan.tenant_id, "dca_proposal", key)
    return "proposed"
