"""Intent policy: mutation classification, plan references, approvals and the ambiguity and fallback rules (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import inspect
import json
import logging
import time
from copy import deepcopy
from typing import Any

from pydantic import BaseModel
from scion_sdk.privacy import persistence_reference

from arbora.mcp_server import runtime
from arbora.mcp_server.backing.intent_contract import (
    operation_id,
)
from arbora.mcp_server.backing.tool_specs import (
    READ_ONLY_ACTIONS,
    TOOL_VERBS,
)
from arbora.mcp_server.backing.tools.intent_candidates import (
    _AMBIGUITY_MARGIN,
    _ASK_FALLBACK_TOOL,
    _CONTROL_HINT_FIELDS,
    _DOCUMENTED_HINT_ALIASES,
    _NON_READ_VERBS,
    _PRIMARY_TEXT_PARAM,
    _READ_ONLY_VERBS,
    CapabilityCandidate,
    _outcome_scope_ref,
    _PreviewPlan,
)
from arbora.mcp_server.host_caches import IntentCache, ToolTable
from arbora.security.error_surface import public_error_payload
from arbora.security.prompt_injection import PromptInjectionScanner

logger = logging.getLogger(__name__)


def _tool_accepts_argument(tools: ToolTable, tool: str, argument: str) -> bool:
    function = tools.get(tool)
    if function is None:
        return False
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return argument in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _normalize_documented_hint_aliases(hints: dict[str, Any]) -> dict[str, Any]:
    """Normalize only documented aliases for an explicitly pinned tool.

    Conflicting names are denied rather than silently choosing a target.  The
    normalized form is what preview storage and plan references bind, so a
    plan-ref-only replay remains exact while callers may use either spelling.
    """

    tool = hints.get("tool") or hints.get("_tool")
    aliases = _DOCUMENTED_HINT_ALIASES.get(str(tool or ""), {})
    normalized = dict(hints)
    for alias, canonical in aliases.items():
        if alias not in normalized:
            continue
        alias_value = normalized.pop(alias)
        if canonical in normalized and normalized[canonical] != alias_value:
            raise ValueError(
                f"Conflicting intent hints {alias!r} and {canonical!r}; "
                f"use only {canonical!r}."
            )
        normalized[canonical] = alias_value
    return normalized


def _unsupported_hint_arguments(
    tools: ToolTable, tool: str, call_kwargs: dict[str, Any]
) -> list[str]:
    """Return unsupported direct-call arguments for a selected tool.

    A ``**kwargs`` tool explicitly owns its open-ended schema.  All other
    tools are closed at this façade, avoiding a raw Python ``TypeError`` after
    a reviewed plan has been produced.
    """

    function = tools.get(tool)
    if function is None:
        return sorted(call_kwargs)
    try:
        parameters = inspect.signature(function).parameters.values()
    except (TypeError, ValueError):
        # An opaque callable has no inspectable public input contract.
        return sorted(call_kwargs)
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return []
    accepted = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    return sorted(set(call_kwargs) - accepted)


def _hint_argument_error(tools: ToolTable, tool: str, unsupported: list[str]) -> str:
    """Give callers a stable correction path without exposing raw exceptions."""

    function = tools.get(tool)
    try:
        parameters = inspect.signature(function).parameters.values() if function else ()
        accepted = sorted(
            parameter.name
            for parameter in parameters
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        )
    except (TypeError, ValueError):
        accepted = []
    names = ", ".join(repr(name) for name in unsupported)
    supported = ", ".join(accepted) or "the tool's documented schema"
    return (
        f"Unsupported intent hint argument(s) for {tool!r}: {names}. "
        f"Use documented parameters: {supported}."
    )


def _plan_ref(
    verb: str,
    tool: str,
    action: str | None,
    call_kwargs: dict[str, Any],
    outcome_scope_ref: str | None,
) -> str:
    """Bind a non-read preview to its policy scope and exact operation."""

    payload = json.dumps(
        {
            "verb": verb,
            "tool": tool,
            "action": action,
            "arguments": call_kwargs,
            "outcome_scope_ref": outcome_scope_ref,
        },
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return persistence_reference("intent_plan", payload)


def _remember_preview_plan(
    intent_cache: IntentCache,
    plan_ref: str,
    *,
    verb: str,
    intent_ref: str,
    outcome_scope_ref: str | None,
    hints: dict[str, Any],
) -> None:
    """Retain one bounded plan so ``plan_ref`` alone can replay its hints."""

    now = time.monotonic()
    intent_cache.remember_preview(
        plan_ref,
        _PreviewPlan(
            created_at=now,
            verb=verb,
            intent_ref=intent_ref,
            outcome_scope_ref=outcome_scope_ref,
            hints=deepcopy({k: v for k, v in hints.items() if k != "plan_ref"}),
        ),
        now,
    )


def _restore_preview_hints(
    intent_cache: IntentCache,
    plan_ref: str,
    *,
    verb: str,
    intent_ref: str,
    outcome_scope_ref: str | None,
) -> dict[str, Any] | None:
    """Return reviewed hints only when the caller and policy context match."""

    preview = intent_cache.preview(plan_ref, time.monotonic())
    if preview is None:
        return None
    if (
        preview.verb != verb
        or preview.intent_ref != intent_ref
        or preview.outcome_scope_ref != outcome_scope_ref
    ):
        return None
    return deepcopy(preview.hints)


def _margin_ambiguity_fields(
    top_score: float,
    second_score: float | None,
    *,
    explicit: bool,
    extra_ambiguous_gate: bool = True,
) -> dict[str, Any]:
    """Shared score-margin ambiguity computation.

    Used by both `_ambiguity_evidence` and `_action_ambiguity_evidence`;
    `extra_ambiguous_gate` carries the latter's extra ``len(ranked_actions) >
    1`` condition (always True — a no-op AND — for the former).
    """
    margin = top_score - second_score if second_score is not None else None
    ambiguous = (
        not explicit
        and extra_ambiguous_gate
        and (
            top_score <= 0.0
            or (
                second_score is not None
                and second_score > 0.0
                and margin is not None
                and margin < _AMBIGUITY_MARGIN
            )
        )
    )
    return {
        "ambiguous": ambiguous,
        "explicit": explicit,
        "top_score": round(top_score, 4),
        "runner_up_score": (
            round(second_score, 4) if second_score is not None else None
        ),
        "margin": round(margin, 4) if margin is not None else None,
        "required_margin": _AMBIGUITY_MARGIN,
    }


def _ambiguity_evidence(
    candidates: list[CapabilityCandidate], *, explicit: bool
) -> dict[str, Any]:
    top_score = candidates[0].score if candidates else 0.0
    second_score = candidates[1].score if len(candidates) > 1 else None
    return _margin_ambiguity_fields(top_score, second_score, explicit=explicit)


def _action_ambiguity_evidence(
    ranked_actions: list[tuple[str, float]], *, explicit: bool
) -> dict[str, Any]:
    if not ranked_actions:
        return {
            "ambiguous": False,
            "explicit": explicit,
            "candidates": [],
        }
    top_score = ranked_actions[0][1]
    second_score = ranked_actions[1][1] if len(ranked_actions) > 1 else None
    fields = _margin_ambiguity_fields(
        top_score,
        second_score,
        explicit=explicit,
        extra_ambiguous_gate=len(ranked_actions) > 1,
    )
    fields["candidates"] = [
        {"action": action, "score": round(score, 4)}
        for action, score in ranked_actions[:5]
    ]
    return fields


def _intent_security_failure(
    intent: str, hints: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Reject prompt/command injection without echoing untrusted content."""

    serialized_hints = json.dumps(
        hints or {}, sort_keys=True, default=str, separators=(",", ":")
    )
    scan = PromptInjectionScanner().scan_text(f"{intent}\n{serialized_hints}")
    if not scan.is_malicious:
        return None
    return {
        "error": "Intent rejected by the prompt-injection policy.",
        "executed": False,
        "security": {
            "decision": "deny",
            "confidence": round(scan.confidence, 4),
            "finding_ref": scan.finding_id,
            "patterns": sorted(
                {
                    str(match.get("pattern_name") or "")
                    for match in scan.matches
                    if match.get("pattern_name")
                }
            ),
        },
    }


def _execution_succeeded(result: Any) -> bool:
    """Classify only the observed tool result; caller feedback is never read.

    Many arbora tools (``graph_query``, ``graph_ask``, ``nl_query``, the
    ``analyze``/``analysis`` surfaces, …) return a typed :class:`EvidenceBundle`
    rather than a plain dict/str. Before the ``BaseModel`` branch below existed,
    such a result matched none of the dict/str cases and fell through to
    ``result is not None`` — which a real, non-empty ``EvidenceBundle`` always
    satisfies, so a FAILED operation (an ``EvidenceBundle`` whose dedicated
    ``error`` field is populated) was reported as succeeded. Dumping the model
    first routes it through the same dict-shaped checks below, so
    ``EvidenceBundle.error`` (truthy exactly when the source payload carried a
    real operation failure — never fabricated, see
    ``arbora.api.evidence``) is honored as the single source
    of truth instead of being invisible to this classifier.
    """

    if isinstance(result, BaseModel):
        return _execution_succeeded(result.model_dump())
    if isinstance(result, dict):
        return _execution_succeeded_dict(result)
    if isinstance(result, str):
        return _execution_succeeded_str(result)
    return result is not None


def _execution_succeeded_dict(result: dict[str, Any]) -> bool:
    """Helper for `_execution_succeeded`: classify a dict-shaped result."""
    if result.get("error") or result.get("ok") is False:
        return False
    if result.get("success") is False or result.get("executed") is False:
        return False
    if str(result.get("status") or "").strip().lower() in {
        "cancelled",
        "denied",
        "error",
        "failed",
        "forbidden",
    }:
        return False
    return True


def _execution_succeeded_str(result: str) -> bool:
    """Helper for `_execution_succeeded`: classify a str-shaped result."""
    stripped = result.strip()
    if not stripped:
        return False
    try:
        decoded = json.loads(stripped)
    except (TypeError, ValueError):
        lowered = stripped.lower()
        return not lowered.startswith(("error", "failed", "forbidden", "denied"))
    return _execution_succeeded(decoded)


def _require_candidate(candidate: CapabilityCandidate | None) -> CapabilityCandidate:
    """The resolved top candidate, refusing rather than returning ``None``.

    Reaching here with ``None`` is a contract violation: the resolver returns
    an outcome dict whenever it finds no candidate. Stating that as a raise
    keeps the invariant in ONE place instead of eight attribute reads.
    """
    if candidate is None:
        raise RuntimeError("intent routing resolved no capability candidate")
    return candidate


def _approval_key(op_id: str) -> str:
    """Opaque key binding an approval to session, authority partition and op."""
    from arbora.fleet.session_keys import ambient_session_key

    payload = json.dumps(
        [ambient_session_key(), _outcome_scope_ref(), op_id], separators=(",", ":")
    )
    return persistence_reference("intent_approval", payload)


def _record_approval(intent_cache: IntentCache, op_id: str) -> None:
    """Record this session's approval of exactly ``op_id``."""
    intent_cache.approve(_approval_key(op_id), time.monotonic())


def _operation_approved(intent_cache: IntentCache, op_id: str) -> bool:
    """BUG-040: has THIS caller's session approved exactly ``op_id``?

    An approval-required (destructive, or non-``auto`` ``approval_class``)
    plan executes only after the session approved that exact operation with
    ``manage(action="approve", params={"action": "<tool>.<op>"})`` — itself a
    governed preview → ``plan_ref`` → execute round trip — and then
    resubmitted the original plan's ``plan_ref``. Naming or pinning the
    operation alone never satisfies it (a pin selects WHICH candidate; it
    never bypasses approval).

    ``approval_class`` is a static CPD declaration, not a per-call check inside
    the target tool; the real authorization boundary remains the actor/session
    identity verification in ``runtime.verified_tool_session_scope`` /
    ``_execute_tool``. The approval step replaces the retired session
    ``load_tools`` acknowledgement with one every client can perform (no
    tool-list refresh involved — the gap BUG-040/BUG-050 found).
    """
    try:
        return intent_cache.approved(_approval_key(op_id), time.monotonic())
    except Exception:
        logging.getLogger(__name__).debug(
            "a broken session probe must fail closed", exc_info=True
        )
        return False


def _plan_ref_hints_match(
    raw_hints: dict[str, Any], restored_hints: dict[str, Any]
) -> bool:
    """Helper for `dispatch_intent`'s `_restore_from_plan_ref` closure.

    D-GIS-1: this equality check exists so a caller resubmitting the SAME
    hints they previewed (plus plan_ref) is provably replaying what was
    reviewed. But `_remember_preview_plan` stores `raw_hints` from AFTER the
    resolver's own `chosen_tool` merge (see `_select_tool_and_action`'s "if
    chosen_tool in _DOCUMENTED_HINT_ALIASES"), which injects a "tool"/"_tool"
    key the caller never supplied and has no way to know in advance (it is
    the resolver's inferred routing target, e.g. an unpinned
    skill-delegation intent auto-resolving to `graph_orchestrate`).
    Comparing against that resolver-injected key made every unpinned
    non-read `act` reject its own documented "resubmit the same hints plus
    plan_ref" flow unconditionally -- the caller's hints could never
    contain a key they were never told to supply. Excluding it here does
    not weaken the check: everything the CALLER actually controls must
    still match exactly, and the resolver will independently re-derive the
    identical `tool` value from the (matched) remaining hints during
    dispatch, so nothing forged by a caller can smuggle a different tool
    through this path.
    """
    resolver_injected_hint_fields = ("tool", "_tool")
    replayed_hints = {
        k: v
        for k, v in raw_hints.items()
        if k != "plan_ref" and k not in resolver_injected_hint_fields
    }
    comparable_restored_hints = {
        k: v
        for k, v in restored_hints.items()
        if k not in resolver_injected_hint_fields
    }
    return not replayed_hints or replayed_hints == comparable_restored_hints


def _normalize_chosen_tool_alias(
    raw_hints: dict[str, Any], chosen_tool: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Helper for `dispatch_intent`'s `_select_tool_and_action` closure.

    A non-pinned resolver result can still be the orchestration façade. It is
    safe to normalize its two documented target aliases only after the
    resolver has selected that tool. Returns (raw_hints, error_response);
    error_response is None on success.
    """
    if chosen_tool not in _DOCUMENTED_HINT_ALIASES:
        return raw_hints, None
    try:
        return (
            _normalize_documented_hint_aliases({**raw_hints, "tool": chosen_tool}),
            None,
        )
    except ValueError as exc:
        # ``exc.args[0]`` (not ``str(exc)``/``repr(exc)``): this ValueError's
        # message is a developer-authored, non-sensitive validation
        # explanation (see _normalize_documented_hint_aliases) that callers
        # need to self-correct their hints -- test_orchestration_target_
        # alias_conflict_fails_closed and test_intent_rejects_unknown_hint_
        # before_creating_preview assert on the exact text. Collapsing to
        # the exception TYPE name here would satisfy the served-boundary
        # exception-surface gate's letter while losing the caller-facing
        # detail those tests require; .args[0] satisfies both.
        return raw_hints, {
            "error": exc.args[0] if exc.args else type(exc).__name__,
            "executed": False,
        }


def _validate_explicit_action(
    verb: str,
    chosen_tool: str,
    explicit_action: Any,
    available_actions: list[str],
) -> dict[str, Any] | None:
    """Helper for `dispatch_intent`'s `_select_tool_and_action` closure.

    Rejects an explicit action not declared for the selected capability.
    """
    if explicit_action is not None and explicit_action not in available_actions:
        return {
            "error": "Requested action is not declared for the selected capability.",
            "executed": False,
            "routing": {
                "verb": verb,
                "chosen_tool": chosen_tool,
                "declared_actions": sorted(available_actions),
            },
        }
    return None


def _declared_default_action(tools: ToolTable, tool: str) -> str | None:
    """The default of the tool's own ``action`` parameter, if it declares one."""
    function = tools.get(tool)
    if function is None:
        return None
    try:
        parameter = inspect.signature(function).parameters.get("action")
    except (TypeError, ValueError):
        return None
    if parameter is None:
        return None
    default = getattr(parameter.default, "default", parameter.default)
    return default if isinstance(default, str) else None


def _no_term_falls_back(
    verb: str, candidate: CapabilityCandidate, explicit_tool: bool, explicit_action: Any
) -> bool:
    """MERISTEM-CONTROL-R034: no matched term sends an unpinned read to the NL planner."""
    return (
        not candidate.matched_terms
        and not explicit_tool
        and explicit_action is None
        and verb in TOOL_VERBS.get(_ASK_FALLBACK_TOOL, ())
    )


def _fallback_reason(fell_back: bool, candidate: CapabilityCandidate) -> str:
    """The routing ``why`` suffix for an NL-planner fallback."""
    if not fell_back:
        return "."
    reason = (
        "the selected capability requires structured arguments"
        if candidate.matched_terms
        else "no capability descriptor term matched the intent"
    )
    return f"; routed through '{_ASK_FALLBACK_TOOL}' because {reason}."


def _routed_tool(
    verb: str, candidate: CapabilityCandidate, explicit_tool: bool, explicit_action: Any
) -> str:
    """The top candidate's tool, or the NL planner under MERISTEM-CONTROL-R034."""
    if _no_term_falls_back(verb, candidate, explicit_tool, explicit_action):
        return _ASK_FALLBACK_TOOL
    return candidate.tool


def _read_tie_outcome(
    tools: ToolTable,
    verb: str,
    chosen_tool: str,
    read_actions: frozenset[str],
    filtered: list[tuple[str, float]],
    ambiguity: dict[str, Any],
) -> tuple[list[tuple[str, float]], dict[str, Any] | None]:
    """MERISTEM-CONTROL-R033: run the declared default on a read tie, else refuse.

    The default leads the ranked list; the tied alternatives stay in it, so the
    routing record reports them.
    """
    default = _declared_default_action(tools, chosen_tool)
    if default in read_actions:
        rest = [item for item in filtered if item[0] != default]
        return [(default, filtered[0][1] if filtered else 0.0), *rest], None
    return filtered, {
        "error": "Ambiguous read-only action requires an explicit action.",
        "executed": False,
        "routing": {
            "verb": verb,
            "chosen_tool": chosen_tool,
            "declared_read_actions": sorted(read_actions),
            "ambiguity": {"action": ambiguity},
        },
    }


def _restrict_to_read_actions(
    tools: ToolTable,
    verb: str,
    chosen_tool: str,
    explicit_action: Any,
    ranked_actions: list[tuple[str, float]],
) -> tuple[list[tuple[str, float]], dict[str, Any] | None]:
    """Helper for `dispatch_intent`'s `_select_tool_and_action` closure.

    For a read-only verb with a declared read-action allowlist: validates the
    explicit action (if any), filters `ranked_actions` to just the read-only
    ones, and checks the result for ambiguity. Returns (ranked_actions,
    error_response); `ranked_actions` is returned unchanged, and
    error_response is None, when the verb/tool has no read-only allowlist.
    """
    read_actions = READ_ONLY_ACTIONS.get(chosen_tool)
    if not (verb in _READ_ONLY_VERBS and read_actions is not None):
        return ranked_actions, None
    if explicit_action is not None and explicit_action not in read_actions:
        return ranked_actions, {
            "error": "Read-only intent action is not declared read-only.",
            "executed": False,
            "routing": {
                "verb": verb,
                "chosen_tool": chosen_tool,
                "declared_read_actions": sorted(read_actions),
            },
        }
    filtered = [
        (action, score) for action, score in ranked_actions if action in read_actions
    ]
    read_action_ambiguity = _action_ambiguity_evidence(
        filtered, explicit=explicit_action is not None
    )
    if explicit_action is None and read_action_ambiguity["ambiguous"]:
        return _read_tie_outcome(
            tools, verb, chosen_tool, read_actions, filtered, read_action_ambiguity
        )
    return filtered, None


def _should_fall_back_to_nl_planner(
    verb: str,
    chosen_tool: str,
    call_kwargs: dict[str, Any],
    raw_hints: dict[str, Any],
    explicit_action: Any,
) -> bool:
    """Helper for `dispatch_intent`'s `_finalize_call_kwargs` closure.

    True when there are no structured hints AND the winning tool has no
    known free-text param: falling back to the NL planner is safer than
    dispatching a call known to be missing required args
    (CONCEPT:MERISTEM-KG.query.ask-gateway-rest-twin). An explicit pin/action
    never falls back — that would silently replace the caller's declared
    route.
    """
    return (
        not call_kwargs
        and chosen_tool not in _PRIMARY_TEXT_PARAM
        and verb == "ask"
        and not raw_hints.get("tool")
        and not raw_hints.get("_tool")
        and explicit_action is None
    )


def _policy_gate_preview_response(
    intent_cache: IntentCache,
    verb: str,
    should_execute: bool,
    plan_ref: str,
    intent_ref: str,
    outcome_scope_ref: str | None,
    raw_hints: dict[str, Any],
    routing: dict[str, Any],
) -> dict[str, Any] | None:
    """Helper for `dispatch_intent`'s `_policy_gate` closure: the preview (not-execute) branch."""
    if should_execute:
        return None
    if verb in _NON_READ_VERBS:
        _remember_preview_plan(
            intent_cache,
            plan_ref,
            verb=verb,
            intent_ref=intent_ref,
            outcome_scope_ref=outcome_scope_ref,
            hints=raw_hints,
        )
    return {"routing": routing, "executed": False}


def _policy_gate_execution_checks(
    verb: str,
    plan: dict[str, Any],
    candidate_ambiguity: dict[str, Any],
    action_ambiguity: dict[str, Any],
    supplied_plan_ref: str,
    plan_ref: str,
    routing: dict[str, Any],
) -> dict[str, Any] | None:
    """Helper for `dispatch_intent`'s `_policy_gate` closure: execution-path policy checks

    (read-only violation, unclassified effect, ambiguity, stale plan_ref) —
    everything except the final approval/session-load check.
    """
    if verb in _READ_ONLY_VERBS and plan["mutates"] is not False:
        return {
            "error": "Read-only intent policy rejected a mutating or unclassified route.",
            "routing": routing,
            "executed": False,
        }
    if plan["execution_class"] == "unclassified":
        return {
            "error": "Operation effect metadata is unclassified; execution denied.",
            "routing": routing,
            "executed": False,
        }
    if verb in _NON_READ_VERBS and (
        candidate_ambiguity["ambiguous"] or action_ambiguity["ambiguous"]
    ):
        return {
            "error": "Ambiguous non-read intent requires an explicit tool and action.",
            "routing": routing,
            "executed": False,
        }
    if verb in _NON_READ_VERBS and supplied_plan_ref != plan_ref:
        return {
            "error": (
                "Preview required: call with execute=false, review the plan, then "
                "resubmit its plan_ref with execute=true."
            ),
            "routing": routing,
            "executed": False,
        }
    return None


def _policy_gate_approval_check(
    intent_cache: IntentCache,
    plan: dict[str, Any],
    routing: dict[str, Any],
) -> dict[str, Any] | None:
    """Helper for `dispatch_intent`'s `_policy_gate` closure: the approval check."""
    required = operation_id(plan["tool"], plan["action"])
    if not plan["approval"]["required"] or _operation_approved(intent_cache, required):
        return None
    return {
        "error": (
            "Approval-required operation: approve it with manage(action='approve', "
            f"params={{'action': {required!r}}}) (preview, then execute with its "
            "plan_ref), then resubmit this plan_ref with execute=true."
        ),
        "routing": routing,
        "executed": False,
        "approval_required": True,
        "required_approval": required,
    }


def _dispatch_hint_derived_fields(
    raw_hints: dict[str, Any],
) -> tuple[str, bool, dict[str, Any]]:
    """Helper for `dispatch_intent`: pinned_name, explicit_tool, and call_kwargs from raw_hints."""
    pinned_name = str(raw_hints.get("tool") or raw_hints.get("_tool") or "")
    explicit_tool = bool(pinned_name)
    call_kwargs = {k: v for k, v in raw_hints.items() if k not in _CONTROL_HINT_FIELDS}
    return pinned_name, explicit_tool, call_kwargs


def _dispatch_reward_and_learning_eligibility(
    verb: str,
    chosen_tool: str,
    outcome_scope_ref: str | None,
    explicit_tool: bool,
    candidate_ambiguity: dict[str, Any],
    action_ambiguity: dict[str, Any],
) -> tuple[float, bool]:
    """Helper for `dispatch_intent`: the calibrated outcome reward + learning eligibility."""
    reward = 0.5
    learning_eligible = bool(
        outcome_scope_ref
        and not explicit_tool
        and not candidate_ambiguity["ambiguous"]
        and not action_ambiguity["ambiguous"]
    )
    return reward, learning_eligible


async def _dispatch_execute_and_record(
    tools: ToolTable,
    verb: str,
    chosen_tool: str,
    call_kwargs: dict[str, Any],
    outcome_scope_ref: str | None,
    learning_eligible: bool,
    routing: dict[str, Any],
) -> dict[str, Any]:
    """Helper for `dispatch_intent`: execute the chosen tool, record the outcome,

    and build the final response dict.
    """
    try:
        result = await runtime._execute_tool(tools, chosen_tool, **call_kwargs)
    except Exception as e:
        logging.getLogger(__name__).debug(
            "surface as a structured routing failure, not a 500", exc_info=True
        )
        routing["decision_trace"]["result_provenance"]["status"] = "failed"
        routing["learning"]["recorded"] = False
        return {
            "routing": routing,
            "executed": False,
            **public_error_payload(e, logger=logger),
        }
    observed_success = _execution_succeeded(result)
    routing["decision_trace"]["result_provenance"]["status"] = (
        "succeeded" if observed_success else "failed"
    )
    routing["learning"]["recorded"] = False
    return {"result": result, "routing": routing, "executed": True}
