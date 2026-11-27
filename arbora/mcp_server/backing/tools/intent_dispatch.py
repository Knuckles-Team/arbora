"""Intent dispatch: the one dispatch of a resolved intent, with its policy gates, execution and learning record (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import logging
from typing import Any

from scion_sdk.privacy import persistence_reference

from arbora.api.domain.fleet.ranking import (
    run_with_port,
)
from arbora.mcp_server.backing.tools.intent_candidates import (
    _ASK_FALLBACK_TOOL,
    _CALLER_OUTCOME_FIELDS,
    _DISPATCH_VERBS,
    _NON_READ_VERBS,
    _PRIMARY_TEXT_PARAM,
    _READ_ONLY_VERBS,
    CapabilityCandidate,
    _actions_by_tool,
    _build_candidates,
    _operation_plan,
    _order_actions,
    _outcome_scope_ref,
    _port_of,
    resolve_intent,
)
from arbora.mcp_server.backing.tools.intent_policy import (
    _action_ambiguity_evidence,
    _ambiguity_evidence,
    _dispatch_execute_and_record,
    _dispatch_hint_derived_fields,
    _dispatch_reward_and_learning_eligibility,
    _fallback_reason,
    _hint_argument_error,
    _intent_security_failure,
    _normalize_chosen_tool_alias,
    _normalize_documented_hint_aliases,
    _plan_ref,
    _plan_ref_hints_match,
    _policy_gate_approval_check,
    _policy_gate_execution_checks,
    _policy_gate_preview_response,
    _require_candidate,
    _restore_preview_hints,
    _restrict_to_read_actions,
    _routed_tool,
    _should_fall_back_to_nl_planner,
    _tool_accepts_argument,
    _unsupported_hint_arguments,
    _validate_explicit_action,
)
from arbora.mcp_server.host_caches import HostCaches

logger = logging.getLogger(__name__)


def _question_routing(intent_ref: str, route: str, why: str) -> dict[str, Any]:
    return {
        "verb": "ask",
        "intent_ref": intent_ref,
        "chosen_tool": route,
        "action": None,
        "why": why,
        "capability_source": "arbora_question_route",
        "decision_trace": {
            "policy": {"verb_class": "read_only", "read_only_enforced": True},
            "route": {"tool": route, "action": None, "fallback": False},
            "result_provenance": {"execution_core": route, "status": "succeeded"},
        },
    }


async def _question_route(
    verb: str, intent: str, raw_hints: dict[str, Any], intent_ref: str
) -> dict[str, Any] | None:
    """Route an unpinned ``ask`` that is a planning or cross-source question.

    Arbora holds no planner and never derives a plan from free text
    (MEDULLA-CQ-ORCH-R001). A plan is the engine's ``TaskPlan`` decision over
    explicit tasks, served and executed by the registry operation
    ``agents.plan_run`` (ARBORA-RELEASE-R003.4.1), which capability ranking
    resolves like any other operation. So a bare ``ask`` is not intercepted
    here: it returns ``None`` and proceeds to ranking. Any hint (a pinned
    tool, an action or structured arguments) keeps the caller's declared
    route.
    """
    return None


async def dispatch_intent(
    caches: HostCaches,
    verb: str,
    intent: str,
    *,
    hints: dict[str, Any] | None = None,
    execute: bool | None = None,
    top_k: int = 5,
    mcp: Any = None,
) -> dict[str, Any]:
    """Resolve, preview, policy-check, and optionally dispatch one intent.

    ``ask``/``why`` execute by default and remain read-only. Non-read verbs
    preview by default; execution requires the opaque ``plan_ref`` returned by
    that exact preview. An approval-required (destructive or non-``auto``
    ``approval_class``) plan only executes once the caller's session approved
    that exact operation (BUG-040 — see :func:`_operation_approved`); otherwise
    it fails closed with an actionable ``required_approval`` hint. Outcome
    learning consumes only the verified tool result of an unpinned,
    unambiguous execution in the current tenant/policy partition.
    """
    raw_hints: dict[str, Any] = {}

    def _intake() -> dict[str, Any] | None:
        nonlocal raw_hints
        try:
            raw_hints = _normalize_documented_hint_aliases(dict(hints or {}))
        except ValueError as exc:
            # ``exc.args[0]`` (not ``str(exc)``/``repr(exc)``): see the identical
            # comment on the second _normalize_documented_hint_aliases call site
            # below.
            return {
                "error": exc.args[0] if exc.args else type(exc).__name__,
                "executed": False,
            }
        if verb not in _DISPATCH_VERBS:
            return {
                "error": "Unsupported Arbora intent verb.",
                "executed": False,
                "routing": {"verb": verb, "candidates": []},
            }
        security_failure = _intent_security_failure(intent, raw_hints)
        if security_failure is not None:
            return security_failure
        supplied_outcomes = sorted(set(raw_hints) & _CALLER_OUTCOME_FIELDS)
        if supplied_outcomes:
            return {
                "error": "Caller-supplied routing outcomes are forbidden.",
                "executed": False,
                "security": {
                    "decision": "deny",
                    "fields": supplied_outcomes,
                    "reason": "Only verified execution results update routing rewards.",
                },
            }
        return None

    _intake_outcome = _intake()
    if _intake_outcome is not None:
        return _intake_outcome

    should_execute = verb in _READ_ONLY_VERBS if execute is None else bool(execute)
    intent_ref = persistence_reference("intent", intent)
    outcome_scope_ref = _outcome_scope_ref()
    supplied_plan_ref = str(raw_hints.get("plan_ref") or "")

    async def _restore_from_plan_ref() -> dict[str, Any] | None:
        nonlocal raw_hints
        if not (verb in _NON_READ_VERBS and should_execute and supplied_plan_ref):
            # No reviewed plan to restore: a bare planning or cross-source
            # ``ask`` is answered here, before capability ranking.
            return await _question_route(verb, intent, raw_hints, intent_ref)
        restored_hints = _restore_preview_hints(
            caches.intent,
            supplied_plan_ref,
            verb=verb,
            intent_ref=intent_ref,
            outcome_scope_ref=outcome_scope_ref,
        )
        if restored_hints is None:
            return {
                "error": (
                    "Unknown, expired, or context-mismatched plan_ref; request a "
                    "new preview before execution."
                ),
                "executed": False,
                "routing": {"verb": verb, "intent_ref": intent_ref},
            }
        if not _plan_ref_hints_match(raw_hints, restored_hints):
            return {
                "error": "Supplied hints do not match the reviewed preview plan.",
                "executed": False,
                "routing": {"verb": verb, "intent_ref": intent_ref},
            }
        raw_hints = {**restored_hints, "plan_ref": supplied_plan_ref}
        return None

    _restore_outcome = await _restore_from_plan_ref()
    if _restore_outcome is not None:
        return _restore_outcome

    pinned_name, explicit_tool, call_kwargs = _dispatch_hint_derived_fields(raw_hints)
    explicit_action = raw_hints.get("action")

    candidates: list[CapabilityCandidate] = []
    top: CapabilityCandidate | None = None
    chosen_tool: str = ""

    ranking = _port_of(mcp)

    async def _resolve_candidates() -> dict[str, Any] | None:
        nonlocal candidates, top, chosen_tool
        candidates = await run_with_port(
            ranking,
            resolve_intent,
            caches,
            verb,
            intent,
            hints=raw_hints,
            top_k=max(2, int(top_k)),
        )
        if not candidates:
            if explicit_tool:
                # Two DIFFERENT failure reasons collapse to the same empty
                # `candidates` from `resolve_intent` — distinguish them so the
                # error is actionable instead of implying a verb-specific policy
                # restriction that doesn't exist. A tool the router has never
                # heard of (most commonly a FLEET tool — it carries no
                # Capability Power Descriptor and was never a candidate at all)
                # is not "disallowed for this verb"; it is reached through
                # act(action="fleet.call") instead.
                known_to_intent_surface = any(
                    candidate.tool == pinned_name
                    for candidate in _build_candidates(caches)
                )
                error = (
                    "Pinned capability is not allowed for this intent verb."
                    if known_to_intent_surface
                    else (
                        f"'{pinned_name}' is not a Arbora intent-routable capability "
                        "(no Capability Power Descriptor is registered for it). "
                        "Call a fleet tool with act(action='fleet.call', "
                        f"params={{'tool': '{pinned_name}', 'arguments': {{...}}}})."
                    )
                )
            else:
                error = "No Arbora capability matched the requested intent verb."
            return {
                "error": error,
                "executed": False,
                "routing": {
                    "verb": verb,
                    "intent_ref": intent_ref,
                    "candidates": [],
                },
            }

        top = candidates[0]
        chosen_tool = _routed_tool(verb, top, explicit_tool, explicit_action)
        return None

    _candidates_outcome = await _resolve_candidates()
    if _candidates_outcome is not None:
        return _candidates_outcome

    # `_resolve_candidates` returns an outcome dict whenever it finds no
    # candidate, so `top` is bound past this point by contract. Binding a
    # non-optional name for it says that once, HERE, instead of leaving every
    # later `top.<attr>` to be read as a possible attribute-on-None — which is
    # what the closures below made it, since a closure reads the DECLARED type
    # of a free variable, not a narrowed one.
    chosen_candidate = _require_candidate(top)

    chosen_action: str | None = None
    ranked_actions: list[tuple[str, float]] = []

    async def _select_tool_and_action() -> dict[str, Any] | None:
        nonlocal raw_hints, chosen_tool, chosen_action, ranked_actions
        # A non-pinned resolver result can still be the orchestration façade.  It
        # is safe to normalize its two documented target aliases only after the
        # resolver has selected that tool.
        raw_hints, alias_error = _normalize_chosen_tool_alias(raw_hints, chosen_tool)
        if alias_error is not None:
            return alias_error

        available_actions = _actions_by_tool().get(chosen_tool, [])
        action_error = _validate_explicit_action(
            verb, chosen_tool, explicit_action, available_actions
        )
        if action_error is not None:
            return action_error

        ranked_actions = await run_with_port(
            ranking, _order_actions, chosen_tool, intent
        )
        ranked_actions, read_action_error = _restrict_to_read_actions(
            caches.tools, verb, chosen_tool, explicit_action, ranked_actions
        )
        if read_action_error is not None:
            return read_action_error

        chosen_action = (
            explicit_action
            or chosen_candidate.action
            or (ranked_actions[0][0] if ranked_actions else None)
        )
        return None

    _select_outcome = await _select_tool_and_action()
    if _select_outcome is not None:
        return _select_outcome

    fell_back = False

    def _finalize_call_kwargs() -> dict[str, Any] | None:
        nonlocal chosen_tool, chosen_action, call_kwargs, ranked_actions, fell_back
        text_param = _PRIMARY_TEXT_PARAM.get(chosen_tool)
        fell_back = chosen_tool != chosen_candidate.tool
        if text_param and text_param not in call_kwargs:
            call_kwargs[text_param] = intent
        elif _should_fall_back_to_nl_planner(
            verb, chosen_tool, call_kwargs, raw_hints, explicit_action
        ):
            # No structured hints AND the winning tool has no known free-text param:
            # fall back to the NL planner rather than dispatch a call we know is
            # missing required args (CONCEPT:MERISTEM-KG.query.ask-gateway-rest-twin).
            # An explicit pin/action never falls back because that would silently
            # replace the caller's declared route.
            fell_back = True
            chosen_tool = _ASK_FALLBACK_TOOL
            chosen_action = None
            call_kwargs = {_PRIMARY_TEXT_PARAM[_ASK_FALLBACK_TOOL]: intent}
            ranked_actions = []

        if chosen_action is not None and _tool_accepts_argument(
            caches.tools, chosen_tool, "action"
        ):
            call_kwargs.setdefault("action", chosen_action)

        unsupported_hints = _unsupported_hint_arguments(
            caches.tools, chosen_tool, call_kwargs
        )
        if unsupported_hints:
            return {
                "error": _hint_argument_error(
                    caches.tools, chosen_tool, unsupported_hints
                ),
                "executed": False,
                "routing": {
                    "verb": verb,
                    "intent_ref": intent_ref,
                    "chosen_tool": chosen_tool,
                    "unsupported_hint_arguments": unsupported_hints,
                },
            }
        return None

    _kwargs_outcome = _finalize_call_kwargs()
    if _kwargs_outcome is not None:
        return _kwargs_outcome

    candidate_ambiguity = _ambiguity_evidence(candidates, explicit=explicit_tool)
    action_ambiguity = _action_ambiguity_evidence(
        ranked_actions, explicit=explicit_action is not None
    )
    plan = _operation_plan(verb, chosen_tool, chosen_action, call_kwargs)
    plan_ref = _plan_ref(
        verb, chosen_tool, chosen_action, call_kwargs, outcome_scope_ref
    )
    plan["plan_ref"] = plan_ref

    reward, learning_eligible = _dispatch_reward_and_learning_eligibility(
        verb,
        chosen_tool,
        outcome_scope_ref,
        explicit_tool,
        candidate_ambiguity,
        action_ambiguity,
    )

    def _build_routing() -> dict[str, Any]:
        routing: dict[str, Any] = {
            "verb": verb,
            "intent_ref": intent_ref,
            "chosen_tool": chosen_tool,
            "action": chosen_action,
            "score": round(chosen_candidate.score, 4),
            "matched_terms": chosen_candidate.matched_terms,
            "fell_back_to_nl_planner": fell_back,
            "why": (
                f"'{chosen_candidate.tool}' best matched the {verb!r} intent on descriptor terms "
                f"{chosen_candidate.matched_terms!r}"
                if chosen_candidate.matched_terms
                else f"'{chosen_candidate.tool}' is the highest-ranked capability for verb {verb!r}"
            )
            + _fallback_reason(fell_back, chosen_candidate),
            "alternatives": [
                {"tool": c.tool, "action": c.action, "score": round(c.score, 4)}
                for c in candidates[1:]
            ],
            "capability_source": "packaged_arbora_cpd",
            "calibrated_outcome_reward": round(reward, 4),
            "ambiguity": {
                "capability": candidate_ambiguity,
                "action": action_ambiguity,
            },
            "plan": plan,
            "decision_trace": {
                "evidence": {
                    "matched_terms": chosen_candidate.matched_terms,
                    "candidate_count": len(candidates),
                    "capability_source": "packaged_arbora_cpd",
                },
                "policy": {
                    "verb_class": (
                        "read_only" if verb in _READ_ONLY_VERBS else "non_read"
                    ),
                    "read_only_enforced": verb in _READ_ONLY_VERBS,
                    "preview_required": plan["preview_required"],
                    "approval": plan["approval"],
                },
                "route": {
                    "tool": chosen_tool,
                    "action": chosen_action,
                    "fallback": fell_back,
                },
                "result_provenance": {
                    "execution_core": "arbora_verified_execute_tool",
                    "status": "preview",
                },
            },
            "learning": {
                "eligible": learning_eligible,
                "partition_ref": outcome_scope_ref or "unverified",
                "source": "verified_execution_result_only",
            },
        }
        return routing

    routing = _build_routing()

    def _policy_gate() -> dict[str, Any] | None:
        preview_response = _policy_gate_preview_response(
            caches.intent,
            verb,
            should_execute,
            plan_ref,
            intent_ref,
            outcome_scope_ref,
            raw_hints,
            routing,
        )
        if preview_response is not None:
            return preview_response
        execution_error = _policy_gate_execution_checks(
            verb,
            plan,
            candidate_ambiguity,
            action_ambiguity,
            supplied_plan_ref,
            plan_ref,
            routing,
        )
        if execution_error is not None:
            return execution_error
        return _policy_gate_approval_check(caches.intent, plan, routing)

    _policy_outcome = _policy_gate()
    if _policy_outcome is not None:
        return _policy_outcome

    return await _dispatch_execute_and_record(
        caches.tools,
        verb,
        chosen_tool,
        call_kwargs,
        outcome_scope_ref,
        learning_eligible,
        routing,
    )
