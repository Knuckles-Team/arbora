"""Scale evaluation: gate checks and the one decision computed from observations (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from arbora.fleet.scaling.service_scale_units import (
    MAX_EVIDENCE,
    MAX_REASONS,
    MAX_REPLICAS,
    ActionKind,
    CapacityAxis,
    CapacityObservation,
    ContinuityObservation,
    DecisionEvidence,
    DecisionReason,
    EvidenceKind,
    EvidenceStatus,
    LoadSafetyObservation,
    QuotaBinding,
    QuotaObservation,
    ScaleContractError,
    ScaleDecision,
    ScalePolicy,
    ScaleUnitContract,
    SignalObservation,
    content_digest,
    utc_instant,
)


def _profile_evidence(
    kind: EvidenceKind,
    ref: str,
    digest: str,
    status: Literal["accepted", "missing", "stale", "blocked"],
    code: str,
) -> DecisionEvidence:
    return DecisionEvidence(
        kind=kind, ref=ref, digest=digest, status=EvidenceStatus(status), code=code
    )


def _fresh(observed_at: datetime, expires_at: datetime, now: datetime) -> bool:
    return observed_at <= now < expires_at


def _capacity_map(
    observations: Iterable[CapacityObservation],
) -> dict[CapacityAxis, CapacityObservation]:
    result: dict[CapacityAxis, CapacityObservation] = {}
    for observation in observations:
        if observation.axis in result:
            raise ScaleContractError("duplicate capacity axis observation")
        result[observation.axis] = observation
    return result


def _quota_map(
    observations: Iterable[QuotaObservation],
) -> dict[str, QuotaObservation]:
    result: dict[str, QuotaObservation] = {}
    for observation in observations:
        if observation.quota_ref in result:
            raise ScaleContractError("duplicate quota observation")
        result[observation.quota_ref] = observation
    return result


def _target_replicas(policy: ScalePolicy, value: int) -> int:
    desired = math.ceil(value / policy.target)
    floor = policy.min_replicas
    if floor == 0 and not policy.allow_scale_to_zero:
        floor = 1
    if value == 0 and policy.allow_scale_to_zero:
        desired = 0
    return max(floor, min(policy.max_replicas, desired))


def _check_signal(
    signal: SignalObservation | None,
    contract: ScaleUnitContract,
    current: datetime,
    evidence: list[DecisionEvidence],
    reasons: list[DecisionReason],
) -> None:
    if signal is None:
        reasons.append("signal_missing")
        evidence.append(
            _profile_evidence(
                "signal",
                "signal:missing",
                content_digest({"kind": "signal", "state": "missing"}),
                "missing",
                "signal_missing",
            )
        )
    elif signal.signal != contract.policy.target_signal:
        reasons.append("signal_mismatch")
        evidence.append(
            _profile_evidence(
                "signal",
                signal.source_ref,
                signal.source_digest,
                "blocked",
                "signal_mismatch",
            )
        )
    elif not _fresh(signal.observed_at, signal.expires_at, current):
        reasons.append("signal_stale")
        evidence.append(
            _profile_evidence(
                "signal",
                signal.source_ref,
                signal.source_digest,
                "stale",
                "signal_stale",
            )
        )
    else:
        evidence.append(
            _profile_evidence(
                "signal",
                signal.source_ref,
                signal.source_digest,
                "accepted",
                signal.signal,
            )
        )


def _check_capacity_axes(
    contract: ScaleUnitContract,
    cap_map: dict[CapacityAxis, CapacityObservation],
    current: datetime,
    evidence: list[DecisionEvidence],
    reasons: list[DecisionReason],
) -> None:
    missing_capacity = False
    stale_capacity = False
    for axis in contract.required_capacity_axes:
        observation = cap_map.get(axis)
        if observation is None:
            missing_capacity = True
            evidence.append(
                _profile_evidence(
                    "capacity",
                    f"capacity:{axis}",
                    content_digest({"axis": axis, "state": "missing"}),
                    "missing",
                    "capacity_missing",
                )
            )
        elif not _fresh(observation.observed_at, observation.expires_at, current):
            stale_capacity = True
            evidence.append(
                _profile_evidence(
                    "capacity",
                    observation.source_ref,
                    observation.source_digest,
                    "stale",
                    "capacity_stale",
                )
            )
        else:
            evidence.append(
                _profile_evidence(
                    "capacity",
                    observation.source_ref,
                    observation.source_digest,
                    "accepted",
                    axis,
                )
            )
    if missing_capacity:
        reasons.append("capacity_missing")
    if stale_capacity:
        reasons.append("capacity_stale")


@dataclass
class _QuotaCheckState:
    """Accumulated flags from :func:`_check_quota_binding`, one per :func:`_check_quotas` call."""

    missing_provider_quota: bool = False
    stale_provider_quota: bool = False
    missing_local_quota: bool = False
    stale_local_quota: bool = False
    quota_scope_mismatch: bool = False
    provider_quota_exhausted: bool = False
    local_quota_exhausted: bool = False


def _record_missing_quota(
    binding: QuotaBinding, evidence: list[DecisionEvidence], state: _QuotaCheckState
) -> None:
    """Extracted from :func:`_check_quota_binding`."""
    is_provider = binding.scope == "provider_global"
    if is_provider:
        state.missing_provider_quota = True
    else:
        state.missing_local_quota = True
    evidence.append(
        _profile_evidence(
            "quota",
            binding.quota_ref,
            content_digest({"quota_ref": binding.quota_ref, "state": "missing"}),
            "missing",
            "provider_quota_missing" if is_provider else "quota_missing",
        )
    )


def _record_quota_scope_mismatch(
    quota_observation: QuotaObservation,
    evidence: list[DecisionEvidence],
    state: _QuotaCheckState,
) -> None:
    """Extracted from :func:`_check_quota_binding`."""
    state.quota_scope_mismatch = True
    evidence.append(
        _profile_evidence(
            "quota",
            quota_observation.source_ref,
            quota_observation.source_digest,
            "blocked",
            "quota_scope_mismatch",
        )
    )


def _record_stale_quota(
    binding: QuotaBinding,
    quota_observation: QuotaObservation,
    evidence: list[DecisionEvidence],
    state: _QuotaCheckState,
) -> None:
    """Extracted from :func:`_check_quota_binding`."""
    is_provider = binding.scope == "provider_global"
    if is_provider:
        state.stale_provider_quota = True
    else:
        state.stale_local_quota = True
    evidence.append(
        _profile_evidence(
            "quota",
            quota_observation.source_ref,
            quota_observation.source_digest,
            "stale",
            "provider_quota_stale" if is_provider else "quota_stale",
        )
    )


def _record_accepted_quota(
    binding: QuotaBinding,
    quota_observation: QuotaObservation,
    evidence: list[DecisionEvidence],
    state: _QuotaCheckState,
) -> None:
    """Extracted from :func:`_check_quota_binding`."""
    evidence.append(
        _profile_evidence(
            "quota",
            quota_observation.source_ref,
            quota_observation.source_digest,
            "accepted",
            binding.scope,
        )
    )
    if quota_observation.used >= quota_observation.limit:
        if binding.scope == "provider_global":
            state.provider_quota_exhausted = True
        else:
            state.local_quota_exhausted = True


def _check_quota_binding(
    binding: QuotaBinding,
    quota_map: dict[str, QuotaObservation],
    current: datetime,
    evidence: list[DecisionEvidence],
    state: _QuotaCheckState,
) -> None:
    """Evaluate one quota binding; mutates ``evidence``/``state``.

    Extracted from :func:`_check_quotas`.
    """
    quota_observation = quota_map.get(binding.quota_ref)
    if quota_observation is None:
        _record_missing_quota(binding, evidence, state)
        return
    if quota_observation.scope != binding.scope:
        _record_quota_scope_mismatch(quota_observation, evidence, state)
        return
    if not _fresh(quota_observation.observed_at, quota_observation.expires_at, current):
        _record_stale_quota(binding, quota_observation, evidence, state)
        return
    _record_accepted_quota(binding, quota_observation, evidence, state)


def _quota_state_to_reasons(
    state: _QuotaCheckState, reasons: list[DecisionReason]
) -> None:
    """Translate the accumulated :class:`_QuotaCheckState` into reason codes.

    Extracted from :func:`_check_quotas`.
    """
    if state.missing_provider_quota:
        reasons.append("provider_quota_missing")
    if state.stale_provider_quota:
        reasons.append("provider_quota_stale")
    if state.missing_local_quota:
        reasons.append("quota_missing")
    if state.stale_local_quota:
        reasons.append("quota_stale")
    if state.quota_scope_mismatch:
        reasons.append("quota_scope_mismatch")


def _check_quotas(
    contract: ScaleUnitContract,
    quota_map: dict[str, QuotaObservation],
    current: datetime,
    evidence: list[DecisionEvidence],
    reasons: list[DecisionReason],
) -> tuple[bool, bool]:
    """Returns (provider_quota_exhausted, local_quota_exhausted)."""
    state = _QuotaCheckState()
    for binding in contract.quotas:
        _check_quota_binding(binding, quota_map, current, evidence, state)
    _quota_state_to_reasons(state, reasons)
    return state.provider_quota_exhausted, state.local_quota_exhausted


def _check_continuity(
    contract: ScaleUnitContract,
    continuity: ContinuityObservation | None,
    current: datetime,
    evidence: list[DecisionEvidence],
    reasons: list[DecisionReason],
) -> None:
    if contract.continuity.mode != "stateless":
        if continuity is None:
            reasons.append("continuity_missing")
            evidence.append(
                _profile_evidence(
                    "continuity",
                    "continuity:missing",
                    content_digest({"kind": "continuity", "state": "missing"}),
                    "missing",
                    "continuity_missing",
                )
            )
        elif not _fresh(continuity.observed_at, continuity.expires_at, current):
            reasons.append("continuity_stale")
            evidence.append(
                _profile_evidence(
                    "continuity",
                    continuity.source_ref,
                    continuity.source_digest,
                    "stale",
                    "continuity_stale",
                )
            )
        elif not continuity.ready:
            reasons.append("continuity_not_ready")
            evidence.append(
                _profile_evidence(
                    "continuity",
                    continuity.source_ref,
                    continuity.source_digest,
                    "blocked",
                    "continuity_not_ready",
                )
            )
        else:
            evidence.append(
                _profile_evidence(
                    "continuity",
                    continuity.source_ref,
                    continuity.source_digest,
                    "accepted",
                    contract.continuity.mode,
                )
            )
    elif continuity is not None and not _fresh(
        continuity.observed_at, continuity.expires_at, current
    ):
        reasons.append("continuity_stale")


def _check_safety(
    contract: ScaleUnitContract,
    safety: LoadSafetyObservation | None,
    current: datetime,
    evidence: list[DecisionEvidence],
    reasons: list[DecisionReason],
) -> None:
    if contract.safety_required:
        if safety is None:
            reasons.append("overload_shedding_active")
            evidence.append(
                _profile_evidence(
                    "safety",
                    "safety:missing",
                    content_digest({"kind": "safety", "state": "missing"}),
                    "missing",
                    "overload_shedding_active",
                )
            )
        elif not _fresh(safety.observed_at, safety.expires_at, current):
            reasons.append("overload_shedding_active")
            evidence.append(
                _profile_evidence(
                    "safety",
                    safety.source_ref,
                    safety.source_digest,
                    "stale",
                    "safety_stale",
                )
            )
        else:
            evidence.append(
                _profile_evidence(
                    "safety",
                    safety.source_ref,
                    safety.source_digest,
                    "accepted",
                    "safety",
                )
            )
            if safety.overloaded:
                reasons.append("overload_shedding_active")
            if safety.noisy_neighbor:
                reasons.append("noisy_neighbor")


def _capacity_exhaustion_reasons(
    contract: ScaleUnitContract,
    cap_map: dict[CapacityAxis, CapacityObservation],
    additional: int,
) -> list[DecisionReason]:
    """``["capacity_exhausted"]`` iff any replica demand axis lacks headroom.

    Extracted from :func:`_check_scale_up_feasibility`.
    """
    for demand in contract.replica_demands:
        observation = cap_map[demand.axis]
        if additional * demand.per_replica > observation.available:
            return ["capacity_exhausted"]
    return []


def _quota_growth_reasons(
    contract: ScaleUnitContract,
    quota_map: dict[str, QuotaObservation],
    additional: int,
) -> list[DecisionReason]:
    """Reasons from projecting every quota binding's usage forward by ``additional``.

    Extracted from :func:`_check_scale_up_feasibility`.
    """
    reasons: list[DecisionReason] = []
    for binding in contract.quotas:
        quota_observation = quota_map[binding.quota_ref]
        if binding.scope == "provider_global":
            if binding.multiplicative:
                reasons.append("provider_quota_not_multiplicative")
            if (
                quota_observation.used + additional * binding.per_replica
                > quota_observation.limit
            ):
                reasons.append("provider_quota_exhausted")
        elif (
            quota_observation.used + additional * binding.per_replica
            > quota_observation.limit
        ):
            reasons.append("capacity_exhausted")
    return reasons


def _engine_authority_reasons(
    contract: ScaleUnitContract, cap_map: dict[CapacityAxis, CapacityObservation]
) -> list[DecisionReason]:
    """``["engine_authority_saturated"]`` iff any engine-authority axis is exhausted.

    Extracted from :func:`_check_scale_up_feasibility`.
    """
    for axis in contract.engine_authority_axes:
        if cap_map[axis].available <= 0:
            return ["engine_authority_saturated"]
    return []


def _check_scale_up_feasibility(
    contract: ScaleUnitContract,
    cap_map: dict[CapacityAxis, CapacityObservation],
    quota_map: dict[str, QuotaObservation],
    *,
    desired: int,
    current_replicas: int,
    current: datetime,
    evidence: list[DecisionEvidence],
    provider_quota_exhausted: bool,
    local_quota_exhausted: bool,
) -> ScaleDecision | None:
    """Returns a blocked ScaleDecision if scale-up is infeasible, else None."""
    additional = desired - current_replicas
    reasons: list[DecisionReason] = list(
        _capacity_exhaustion_reasons(contract, cap_map, additional)
    )
    if provider_quota_exhausted:
        reasons.append("provider_quota_exhausted")
    if local_quota_exhausted:
        reasons.append("capacity_exhausted")
    reasons.extend(_quota_growth_reasons(contract, quota_map, additional))
    reasons.extend(_engine_authority_reasons(contract, cap_map))
    if reasons:
        return _decision(
            contract,
            current=current_replicas,
            desired=current_replicas,
            action="blocked",
            reasons=reasons,
            evidence=evidence,
            drain_required=False,
            evaluated_at=current,
        )
    return None


@dataclass(frozen=True)
class _ScaleDecisionContext:
    """The scale-decision inputs threaded through every terminal-decision call.

    Extracted from :func:`_compute_scale_decision`, to avoid repeating the same
    contract/current_replicas/current/evidence quadruple at every call site.
    """

    contract: ScaleUnitContract
    current_replicas: int
    current: datetime
    evidence: list[DecisionEvidence]


def _terminal_decision(
    ctx: _ScaleDecisionContext,
    *,
    action: ActionKind,
    reasons: tuple[DecisionReason, ...],
    drain_required: bool = False,
) -> ScaleDecision:
    """A decision holding replicas at their current count.

    Extracted from :func:`_compute_scale_decision`.
    """
    return _decision(
        ctx.contract,
        current=ctx.current_replicas,
        desired=ctx.current_replicas,
        action=action,
        reasons=reasons,
        evidence=ctx.evidence,
        drain_required=drain_required,
        evaluated_at=ctx.current,
    )


def _check_scale_from_zero(
    ctx: _ScaleDecisionContext, policy: ScalePolicy, action: ActionKind
) -> ScaleDecision | None:
    """Extracted from :func:`_compute_scale_decision`."""
    if (
        action == "scale_up"
        and ctx.current_replicas == 0
        and not policy.allow_scale_from_zero
    ):
        return _terminal_decision(
            ctx, action="blocked", reasons=("scale_from_zero_disabled",)
        )
    return None


def _adjust_scale_to_zero(
    ctx: _ScaleDecisionContext,
    policy: ScalePolicy,
    desired: int,
    reasons: list[DecisionReason],
) -> tuple[int, ScaleDecision | None]:
    """Clamp ``desired`` up when scale-to-zero is disabled; hold if that clamp
    lands back on the current replica count.

    Extracted from :func:`_compute_scale_decision`. ``reasons.append`` here is
    pre-existing DEBT (not created by this diff): ``reasons`` is a write-only
    list in the caller -- every terminal ``_decision``/``_terminal_decision``
    call passes a literal reasons tuple instead of reading it back. Kept
    verbatim, not fixed, per lane scope.
    """
    if desired != 0 or policy.allow_scale_to_zero:
        return desired, None
    desired = max(1, policy.min_replicas)
    if desired == ctx.current_replicas:
        return desired, _terminal_decision(
            ctx, action="hold", reasons=("scale_to_zero_disabled",)
        )
    reasons.append("scale_to_zero_disabled")
    return desired, None


def _check_active_sessions_block_drain(
    ctx: _ScaleDecisionContext, continuity: ContinuityObservation | None
) -> ScaleDecision | None:
    """Extracted from :func:`_compute_scale_decision`."""
    if (
        continuity is not None
        and ctx.contract.continuity.block_scale_down_with_active
        and continuity.active_sessions + continuity.active_streams > 0
    ):
        return _terminal_decision(
            ctx,
            action="blocked",
            reasons=("active_sessions_block_drain",),
            drain_required=True,
        )
    return None


def _evaluate_scale_down_gates(
    ctx: _ScaleDecisionContext,
    policy: ScalePolicy,
    desired: int,
    reasons: list[DecisionReason],
    continuity: ContinuityObservation | None,
) -> tuple[int, ScaleDecision | None]:
    """The scale_down-only gates: zero-clamp, then the active-sessions block.

    Extracted from :func:`_compute_scale_decision` so its own ``if action ==
    "scale_down":`` branch stays flat (one call, not two nested checks).
    """
    desired, blocked = _adjust_scale_to_zero(ctx, policy, desired, reasons)
    if blocked is not None:
        return desired, blocked
    return desired, _check_active_sessions_block_drain(ctx, continuity)


def _check_cooldown(
    ctx: _ScaleDecisionContext,
    policy: ScalePolicy,
    action: ActionKind,
    last_action_at: datetime | None,
) -> ScaleDecision | None:
    """Extracted from :func:`_compute_scale_decision`."""
    if last_action_at is None:
        return None
    previous = utc_instant(last_action_at, name="last_action_at")
    cooldown = (
        policy.scale_up_cooldown_s
        if action == "scale_up"
        else policy.scale_down_cooldown_s
    )
    if (ctx.current - previous).total_seconds() < cooldown:
        return _terminal_decision(ctx, action="hold", reasons=("cooldown",))
    return None


def _check_drain_feasibility(
    ctx: _ScaleDecisionContext, drain_required: bool
) -> ScaleDecision | None:
    """Extracted from :func:`_compute_scale_decision`."""
    if (
        drain_required
        and ctx.contract.policy.drain_seconds
        > ctx.contract.continuity.max_drain_seconds
    ):
        return _terminal_decision(
            ctx, action="blocked", reasons=("drain_required",), drain_required=True
        )
    return None


def _resolve_scale_target(
    ctx: _ScaleDecisionContext, policy: ScalePolicy, raw_desired: int
) -> tuple[int, ActionKind, ScaleDecision | None]:
    """The stepped ``(desired, action)`` target, or the "at_target" hold decision.

    Extracted from :func:`_compute_scale_decision`. The third element is
    ``None`` unless ``raw_desired`` already equals the current replica count.
    """
    if raw_desired > ctx.current_replicas:
        desired = min(ctx.current_replicas + policy.scale_up_step, raw_desired)
        return desired, "scale_up", None
    if raw_desired < ctx.current_replicas:
        desired = max(ctx.current_replicas - policy.scale_down_step, raw_desired)
        return desired, "scale_down", None
    return (
        ctx.current_replicas,
        "hold",
        _terminal_decision(ctx, action="hold", reasons=("at_target",)),
    )


def _finalize_scale_decision(
    ctx: _ScaleDecisionContext,
    cap_map: dict[CapacityAxis, CapacityObservation],
    quota_map: dict[str, QuotaObservation],
    *,
    action: ActionKind,
    desired: int,
    provider_quota_exhausted: bool,
    local_quota_exhausted: bool,
) -> ScaleDecision:
    """The scale_up feasibility gate, the drain-feasibility gate, and (absent a
    block) the final ``target_tracking`` decision.

    Extracted from :func:`_compute_scale_decision`.
    """
    if action == "scale_up":
        blocked = _check_scale_up_feasibility(
            ctx.contract,
            cap_map,
            quota_map,
            desired=desired,
            current_replicas=ctx.current_replicas,
            current=ctx.current,
            evidence=ctx.evidence,
            provider_quota_exhausted=provider_quota_exhausted,
            local_quota_exhausted=local_quota_exhausted,
        )
        if blocked is not None:
            return blocked

    drain_required = action == "scale_down" and ctx.contract.continuity.drain_required
    blocked = _check_drain_feasibility(ctx, drain_required)
    if blocked is not None:
        return blocked
    return _decision(
        ctx.contract,
        current=ctx.current_replicas,
        desired=desired,
        action=action,
        reasons=("target_tracking",),
        evidence=ctx.evidence,
        drain_required=drain_required,
        evaluated_at=ctx.current,
    )


def _compute_scale_decision(
    contract: ScaleUnitContract,
    *,
    current_replicas: int,
    signal: SignalObservation | None,
    cap_map: dict[CapacityAxis, CapacityObservation],
    quota_map: dict[str, QuotaObservation],
    continuity: ContinuityObservation | None,
    last_action_at: datetime | None,
    current: datetime,
    evidence: list[DecisionEvidence],
    provider_quota_exhausted: bool,
    local_quota_exhausted: bool,
) -> ScaleDecision:
    reasons: list[DecisionReason] = []
    if signal is None:
        raise ScaleContractError(
            "signal validation unexpectedly completed without a signal"
        )
    ctx = _ScaleDecisionContext(
        contract=contract,
        current_replicas=current_replicas,
        current=current,
        evidence=evidence,
    )
    policy = contract.policy
    raw_desired = _target_replicas(policy, signal.value)
    desired, action, blocked = _resolve_scale_target(ctx, policy, raw_desired)
    if blocked is not None:
        return blocked

    blocked = _check_scale_from_zero(ctx, policy, action)
    if blocked is not None:
        return blocked

    if action == "scale_down":
        desired, blocked = _evaluate_scale_down_gates(
            ctx, policy, desired, reasons, continuity
        )
        if blocked is not None:
            return blocked

    blocked = _check_cooldown(ctx, policy, action, last_action_at)
    if blocked is not None:
        return blocked

    return _finalize_scale_decision(
        ctx,
        cap_map,
        quota_map,
        action=action,
        desired=desired,
        provider_quota_exhausted=provider_quota_exhausted,
        local_quota_exhausted=local_quota_exhausted,
    )


def evaluate_scale(
    contract: ScaleUnitContract,
    *,
    current_replicas: int,
    signal: SignalObservation | None,
    capacities: Iterable[CapacityObservation],
    quotas: Iterable[QuotaObservation],
    continuity: ContinuityObservation | None,
    safety: LoadSafetyObservation | None,
    now: datetime,
    last_action_at: datetime | None = None,
) -> ScaleDecision:
    """Evaluate one target-tracking decision for any declared service surface.

    Missing/stale dependencies produce ``blocked`` rather than optimistic scale
    actions.  In particular, MCP replicas never multiply a provider-global quota,
    and query readers never claim to fix a saturated engine authority.
    """

    current = utc_instant(now, name="now")
    if current_replicas < 0 or current_replicas > MAX_REPLICAS:
        raise ScaleContractError("current_replicas is outside bounds")
    if current_replicas < contract.policy.min_replicas:
        raise ScaleContractError("current_replicas is below the contract floor")
    cap_map = _capacity_map(capacities)
    quota_map = _quota_map(quotas)
    evidence: list[DecisionEvidence] = []
    reasons: list[DecisionReason] = []

    _check_signal(signal, contract, current, evidence, reasons)
    _check_capacity_axes(contract, cap_map, current, evidence, reasons)
    provider_quota_exhausted, local_quota_exhausted = _check_quotas(
        contract, quota_map, current, evidence, reasons
    )
    _check_continuity(contract, continuity, current, evidence, reasons)
    _check_safety(contract, safety, current, evidence, reasons)

    if last_action_at is not None:
        previous = utc_instant(last_action_at, name="last_action_at")
        if previous > current:
            reasons.append("cooldown")

    if reasons:
        # Dependencies that are not trustworthy block both growth and shrinkage;
        # a controller must not shrink a surface based on stale safety evidence.
        return _decision(
            contract,
            current=current_replicas,
            desired=current_replicas,
            action="blocked",
            reasons=reasons,
            evidence=evidence,
            drain_required=False,
            evaluated_at=current,
        )

    return _compute_scale_decision(
        contract,
        current_replicas=current_replicas,
        signal=signal,
        cap_map=cap_map,
        quota_map=quota_map,
        continuity=continuity,
        last_action_at=last_action_at,
        current=current,
        evidence=evidence,
        provider_quota_exhausted=provider_quota_exhausted,
        local_quota_exhausted=local_quota_exhausted,
    )


def _decision(
    contract: ScaleUnitContract,
    *,
    current: int,
    desired: int,
    action: ActionKind,
    reasons: Iterable[DecisionReason],
    evidence: Iterable[DecisionEvidence],
    drain_required: bool,
    evaluated_at: datetime,
) -> ScaleDecision:
    normalized_reasons = tuple(dict.fromkeys(reasons))[:MAX_REASONS]
    if action == "blocked" and not normalized_reasons:
        normalized_reasons = ("invalid_bounds",)
    return ScaleDecision(
        contract_id=contract.contract_id,
        evaluated_at=evaluated_at,
        current_replicas=current,
        desired_replicas=desired,
        action=action,
        reasons=normalized_reasons,
        drain_required=drain_required,
        evidence=tuple(evidence)[:MAX_EVIDENCE],
    )
