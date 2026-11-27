"""Closed-loop capacity proposals and the applied-state receipt.

ARBORA-CAPACITY-R001.3.1 to R001.3.4 and R751. The throttle controller builds
a bounded ``CapacityAdjustmentProposal`` from a changed AIMD decision, has the
engine judge it, applies only an allowed one and reads the applied state back
from a fresh capacity read. Arbora decides no feasibility: the engine verdict
is the authority, and a missing or failed verdict holds the prior limit.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from arbora.api.domain.fleet.error_budget import (
    Partition,
    ThrottleDecision,
    ThrottleMode,
)

logger = logging.getLogger(__name__)

PROPOSAL_FIELD_INVALID = "PROPOSAL_FIELD_INVALID"
VERDICT_UNAVAILABLE = "VERDICT_UNAVAILABLE"
CAPACITY_EPOCH_CONFLICT = "CAPACITY_EPOCH_CONFLICT"
CAPACITY_APPLY_MISMATCH = "CAPACITY_APPLY_MISMATCH"


class CapacityProposalError(ValueError):
    """A proposal field the engine contract would refuse, caught before the call."""

    def __init__(self, field: str) -> None:
        super().__init__(f"{PROPOSAL_FIELD_INVALID}: {field}")
        self.code = PROPOSAL_FIELD_INVALID
        self.field = field


@dataclass(frozen=True, slots=True)
class CapacityCellView:
    """One engine capacity cell as ``CapacityStatus`` reports it."""

    cell_id: str
    resource_class_iri: str
    unit: str
    capacity: int
    min_capacity: int
    max_capacity: int
    measured_load: int
    available_headroom: int
    premise_revision: int
    replicas: bool = False
    raw: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CapacityAdjustmentProposal:
    """The engine's ``CapacityAdjustmentProposal`` wire record."""

    available_headroom: int
    current_capacity: int
    max_capacity: int
    measured_load: int
    min_capacity: int
    policy_revision: int
    premise_revision: int
    proposal_id: str
    proposed_capacity: int
    resource_class_iri: str
    tenant_id: str
    unit: str
    window_end_ms: int
    window_start_ms: int
    proposal_digest: str = ""

    def wire(self) -> dict[str, Any]:
        return asdict(self)


def _digest(fields: Mapping[str, Any]) -> str:
    body = {k: v for k, v in fields.items() if k not in {"proposal_digest"}}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _non_negative(name: str, value: object) -> None:
    if type(value) is not int or value < 0:
        raise CapacityProposalError(name)


def build_proposal(
    decision: ThrottleDecision,
    cell: CapacityCellView,
    *,
    window_start_ms: int,
    window_end_ms: int,
    policy_revision: int,
) -> CapacityAdjustmentProposal | None:
    """Fill the proposal from a decision and the cell; ``None`` if nothing changes."""
    if decision.new_limit == decision.prior_limit:
        return None
    if not cell.cell_id or not cell.unit:
        raise CapacityProposalError("cell")
    for name, value in (
        ("proposed_capacity", decision.new_limit),
        ("current_capacity", decision.prior_limit),
        ("min_capacity", cell.min_capacity),
        ("max_capacity", cell.max_capacity),
        ("measured_load", cell.measured_load),
        ("available_headroom", cell.available_headroom),
        ("policy_revision", policy_revision),
        ("premise_revision", cell.premise_revision),
        ("window_start_ms", window_start_ms),
        ("window_end_ms", window_end_ms),
    ):
        _non_negative(name, value)
    if window_end_ms < window_start_ms:
        raise CapacityProposalError("window")
    fields = {
        "available_headroom": cell.available_headroom,
        "current_capacity": decision.prior_limit,
        "max_capacity": cell.max_capacity,
        "measured_load": cell.measured_load,
        "min_capacity": cell.min_capacity,
        "policy_revision": policy_revision,
        "premise_revision": cell.premise_revision,
        "proposal_id": "capacity:" + decision.window_digest[:32],
        "proposed_capacity": decision.new_limit,
        "resource_class_iri": cell.resource_class_iri,
        "tenant_id": decision.partition.tenant,
        "unit": cell.unit,
        "window_end_ms": window_end_ms,
        "window_start_ms": window_start_ms,
    }
    return CapacityAdjustmentProposal(**fields, proposal_digest=_digest(fields))


class VerdictKind(StrEnum):
    ALLOW = "allow"
    REFUSE = "refuse"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class CapacityVerdict:
    """The engine's answer, bound to the proposal it judged."""

    kind: VerdictKind
    code: str
    proposal_digest: str
    policy_revision: int
    state_revision: int

    @property
    def allowed(self) -> bool:
        return self.kind is VerdictKind.ALLOW


Judge = Callable[[Mapping[str, Any]], Awaitable[str]]


async def judge(ask: Judge, proposal: CapacityAdjustmentProposal) -> CapacityVerdict:
    """Ask the engine ``capacity_proposal`` question; a failure holds the limit.

    ``ask`` sends the typed ``Decide`` question and returns ``allow`` or
    ``refuse:<CODE>``. Any failure to obtain a verified answer is
    ``VERDICT_UNAVAILABLE``: Arbora never applies a proposal without ``allow``.
    """
    bound = (proposal.policy_revision, proposal.premise_revision)
    try:
        answer = await ask(proposal.wire())
    except Exception:
        logger.warning("judge failed", exc_info=True)
        return CapacityVerdict(
            VerdictKind.UNAVAILABLE,
            VERDICT_UNAVAILABLE,
            proposal.proposal_digest,
            *bound,
        )
    if answer == "allow":
        return CapacityVerdict(
            VerdictKind.ALLOW, "allow", proposal.proposal_digest, *bound
        )
    code = answer.removeprefix("refuse:")
    return CapacityVerdict(VerdictKind.REFUSE, code, proposal.proposal_digest, *bound)


class CapacityPort(Protocol):
    """The engine calls the controller needs; recorded in tests, engine in service."""

    async def cell(self, partition: Partition) -> CapacityCellView: ...

    async def update_cell(self, cell: CapacityCellView, capacity: int) -> str:
        """``accepted``, or ``stale_epoch`` when the cell moved since the read."""
        ...

    async def throttle_cell(
        self, cell: CapacityCellView, errors: int, requests: int, window_end_ms: int
    ) -> None: ...


class ReplicaIntents(Protocol):
    """The scaling-intent path of ARBORA-CAPACITY-R750.752 for a replica change."""

    async def submit(self, proposal: CapacityAdjustmentProposal) -> None: ...


@dataclass(frozen=True, slots=True)
class CapacityApplyResult:
    outcome: str
    requested_capacity: int
    code: str = ""


@dataclass(frozen=True, slots=True)
class CapacityAppliedReceipt:
    """What a fresh read says the cell now holds, bound to the proposal."""

    proposal_digest: str
    requested_capacity: int
    applied_capacity: int
    cell_id: str
    policy_revision: int
    state_revision: int
    outcome: str


async def apply(
    port: CapacityPort,
    proposal: CapacityAdjustmentProposal,
    verdict: CapacityVerdict,
    *,
    mode: ThrottleMode,
    cell: CapacityCellView,
    intents: ReplicaIntents | None = None,
) -> CapacityApplyResult:
    """Apply an allowed proposal; observe mode and a missing allow change nothing."""
    requested = proposal.proposed_capacity
    if not verdict.allowed or verdict.proposal_digest != proposal.proposal_digest:
        return CapacityApplyResult("held", requested, verdict.code)
    if mode is not ThrottleMode.ENFORCE:
        return CapacityApplyResult("observed_only", requested)
    if requested <= cell.max_capacity:
        # Under the engine ceiling the change is the partition limit itself.
        moved = "narrowed" if requested < proposal.current_capacity else "raised"
        return CapacityApplyResult(moved, requested)
    if cell.replicas and intents is not None:
        await intents.submit(proposal)
        return CapacityApplyResult("intent_submitted", requested)
    outcome = await port.update_cell(cell, requested)
    if outcome == "stale_epoch":
        return CapacityApplyResult("held", requested, CAPACITY_EPOCH_CONFLICT)
    return CapacityApplyResult("ceiling_raised", requested)


async def read_back(
    port: CapacityPort,
    partition: Partition,
    proposal: CapacityAdjustmentProposal,
    verdict: CapacityVerdict,
    applied: CapacityApplyResult,
    *,
    limit_now: int,
) -> CapacityAppliedReceipt:
    """The receipt comes from a fresh status read, never from the apply reply.

    ``limit_now`` is the partition limit the controller holds after the apply.
    """
    fresh = await port.cell(partition)
    outcome = applied.outcome
    ceiling_moved = outcome in {"ceiling_raised", "intent_submitted"}
    held_value = fresh.capacity if ceiling_moved else min(limit_now, fresh.capacity)
    wanted = proposal.proposed_capacity if outcome != "held" else limit_now
    if outcome not in {"held", "observed_only"} and held_value != wanted:
        outcome = CAPACITY_APPLY_MISMATCH
    return CapacityAppliedReceipt(
        proposal_digest=proposal.proposal_digest,
        requested_capacity=proposal.proposed_capacity,
        applied_capacity=held_value,
        cell_id=fresh.cell_id,
        policy_revision=verdict.policy_revision,
        state_revision=fresh.premise_revision,
        outcome=outcome,
    )


__all__ = [
    "CAPACITY_APPLY_MISMATCH",
    "CAPACITY_EPOCH_CONFLICT",
    "PROPOSAL_FIELD_INVALID",
    "VERDICT_UNAVAILABLE",
    "CapacityAdjustmentProposal",
    "CapacityAppliedReceipt",
    "CapacityApplyResult",
    "CapacityCellView",
    "CapacityPort",
    "CapacityProposalError",
    "CapacityVerdict",
    "ReplicaIntents",
    "VerdictKind",
    "apply",
    "build_proposal",
    "judge",
    "read_back",
]
