"""The authority of one invoke: the policy decision, the effect plan and the fleet-call checks (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from arbora.api.invoke.executor import (
    prepare_executor,
)
from arbora.api.invoke.gate_class import resolve_gate
from arbora.api.invoke.pipeline_services import (
    _PLAN_GATED_CONFIRMS,
    FleetCallDecision,
    InvokeServices,
)
from arbora.api.invoke.plan import PlanBinding, bind_plan
from arbora.api.invoke.steps import (
    AnonymousCaller,
    RequestCaller,
    authenticate,
    principal_rule,
    require_scopes,
)
from arbora.api.registry import (
    AuditClass,
    Confirm,
    Effect,
    Executor,
    PrincipalRule,
    Surface,
)
from arbora.core.claims import params_digest
from arbora.security.operation_result import OpError, OpResult

_logger = logging.getLogger(__name__)


MFA_FRESH_SECONDS = 900


async def _policy(
    op: Any, caller: RequestCaller, services: InvokeServices
) -> OpError | None:
    try:
        allowed = await services.policy_gate.check_op(op, caller)
    except Exception:
        logging.getLogger(__name__).warning("authority call failed", exc_info=True)
        return OpError("POLICY_UNAVAILABLE")
    return None if allowed is True else OpError("POLICY_DENIED")


async def _base_authority(
    op: Any, caller: RequestCaller, services: InvokeServices
) -> OpError | None:
    for check in (principal_rule, require_scopes):
        refused = check(op, caller)
        if refused is not None:
            return refused
    return await _policy(op, caller, services)


def _anonymous_effect(
    op: Any, params: Mapping[str, Any], caller: RequestCaller
) -> OpError | None:
    """An anonymous effect runs only under an operator policy entry (R004.6)."""
    if not isinstance(caller, AnonymousCaller) or op.effect == Effect.READ:
        return None
    from arbora.security.anonymous import admit_effect

    target = str(params.get("target") or params.get("node_id") or "")
    code = admit_effect(op.id, target, params_digest(params))
    return None if code is None else OpError(code)


def split_envelope(
    op: Any, params: Mapping[str, Any]
) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
    """The params without the op's envelope fields, and those fields (ARBORA-DATA-MARKET-R007.2).

    This is the one place the extension is separated, for every surface: the remainder is
    what validation, the plan digest, audit and reservation see.
    """
    fields = getattr(op, "envelope", ())
    if not fields:
        return params, {}
    kept = {k: v for k, v in params.items() if k not in fields}
    return kept, {k: params[k] for k in fields if params.get(k) is not None}


async def _effect(
    op: Any,
    params: Mapping[str, Any],
    caller: RequestCaller,
    surface: Surface,
    services: InvokeServices,
    *,
    plan_ref: str | None,
    resolved_from_intent: bool,
) -> OpError | OpResult | None:
    refused = _anonymous_effect(op, params, caller)
    if refused is not None:
        return refused
    return await _effect_plan(
        op,
        params,
        caller,
        surface,
        services,
        plan_ref=plan_ref,
        resolved_from_intent=resolved_from_intent,
    )


async def _effect_plan(
    op: Any,
    params: Mapping[str, Any],
    caller: RequestCaller,
    surface: Surface,
    services: InvokeServices,
    *,
    plan_ref: str | None,
    resolved_from_intent: bool,
) -> OpError | OpResult | None:
    if not _requires_plan(op, resolved_from_intent):
        return OpError("PLAN_MISMATCH") if plan_ref is not None else None
    binding = bind_plan(op, params, caller, services.registry.digest)
    if plan_ref is None:
        return await _issue_plan(op, binding, params, services)
    if op.confirm == Confirm.CONSOLE and not _console_ready(caller, surface):
        return OpError("PRINCIPAL_NOT_ALLOWED")
    try:
        return await services.plans.consume(plan_ref, binding)
    except Exception:
        logging.getLogger(__name__).warning("authority call failed", exc_info=True)
        return OpError("UNAVAILABLE", {"reason": "plan lease unavailable"})


async def _issue_plan(
    op: Any,
    binding: PlanBinding,
    params: Mapping[str, Any],
    services: InvokeServices,
) -> OpResult | OpError:
    unavailable = OpError("UNAVAILABLE", {"reason": "plan lease unavailable"})
    try:
        gate = await resolve_gate(services.gate_reader, op.id, binding.tenant)
        reference = await services.plans.issue(binding, params, gate)
    except Exception:
        logging.getLogger(__name__).warning("authority call failed", exc_info=True)
        return unavailable
    if not isinstance(reference, str) or not reference:
        return unavailable
    details = {"plan_ref": reference, "op": op.id, "effect": op.effect.value}
    if op.confirm == Confirm.CONSOLE:
        details["console_url"] = f"/console/confirm/{reference}"
        return OpResult(code="STEP_UP_REQUIRED", details=details)
    return OpResult(code="CONFIRMATION_REQUIRED", details=details)


def _requires_plan(op: Any, resolved_from_intent: bool) -> bool:
    return op.confirm in _PLAN_GATED_CONFIRMS or (
        op.effect == Effect.WRITE and resolved_from_intent
    )


def _console_ready(caller: RequestCaller, surface: Surface) -> bool:
    if (
        surface != Surface.CONSOLE
        or caller.delegated
        or caller.mfa_at_ms is None
        or caller.principal_kind != "human"
        or caller.credential_kind != "session"
    ):
        return False
    import time

    age_ms = int(time.time() * 1000) - caller.mfa_at_ms
    return 0 <= age_ms <= MFA_FRESH_SECONDS * 1000


def _effect_authority_valid(
    effect: Effect, confirm: Confirm, principals: PrincipalRule
) -> bool:
    """Destructive effects need a plan; admin effects need an undelegated human."""
    if effect == Effect.DESTRUCTIVE:
        return confirm == Confirm.PLAN
    if effect == Effect.ADMIN:
        return (
            confirm == Confirm.CONSOLE and principals == PrincipalRule.HUMAN_UNDELEGATED
        )
    return True


def _fleet_types_valid(decision: FleetCallDecision) -> bool:
    if not isinstance(decision.required_scopes, frozenset) or not isinstance(
        decision.executor_scopes, frozenset
    ):
        return False
    typed = all(
        isinstance(value, expected)
        for value, expected in (
            (decision.effect, Effect),
            (decision.confirm, Confirm),
            (decision.principals, PrincipalRule),
            (decision.executor, Executor),
        )
    )
    return typed and all(
        isinstance(scope, str) and bool(scope) and "*" not in scope
        for scope in decision.required_scopes | decision.executor_scopes
    )


def _fleet_executor_valid(op: Any, decision: FleetCallDecision) -> bool:
    if decision.executor == Executor.CALLER:
        return (
            decision.credential_mode == "delegated"
            and not decision.executor_scopes
            and decision.subject_id is None
        )
    return (
        decision.credential_mode == "service"
        and bool(decision.required_scopes)
        and bool(decision.executor_scopes)
        and isinstance(decision.subject_id, str)
        and 1 <= len(decision.subject_id) <= 256
        and op.audit != AuditClass.NONE
    )


def _valid_fleet_decision(op: Any, decision: FleetCallDecision) -> bool:
    return (
        _fleet_types_valid(decision)
        and _effect_authority_valid(
            decision.effect, decision.confirm, decision.principals
        )
        and _fleet_executor_valid(op, decision)
    )


async def _refresh(
    caller: RequestCaller | None, services: InvokeServices
) -> RequestCaller | OpError:
    refused = authenticate(caller)
    if refused is not None:
        return refused
    assert caller is not None
    try:
        current = await services.runtime.verify_current(caller)
    except Exception:
        logging.getLogger(__name__).warning("authority call failed", exc_info=True)
        return OpError("UNAUTHENTICATED")
    refused = authenticate(current)
    if refused is not None:
        return refused
    if (current.principal, current.tenant, current.principal_kind) != (
        caller.principal,
        caller.tenant,
        caller.principal_kind,
    ):
        return OpError("UNAUTHENTICATED")
    current = replace(current, request_id=caller.request_id)
    if current != caller:
        return OpError("UNAUTHENTICATED")
    return current


async def authorize(
    op: Any,
    params: Mapping[str, Any],
    caller: RequestCaller,
    services: InvokeServices,
    decision: FleetCallDecision | None,
) -> OpError | None:
    refreshed = await _refresh(caller, services)
    if isinstance(refreshed, OpError):
        return refreshed
    refused = await _base_authority(op, caller, services)
    if refused is not None:
        return refused
    if services.resource_gate is not None and decision is not None:
        refused = await services.resource_gate(op, params, caller, decision)
        if refused is not None:
            return refused
    return await prepare_executor(
        op,
        params,
        caller,
        services.runtime,
        verified_subject=decision.subject_id if decision else None,
    )
