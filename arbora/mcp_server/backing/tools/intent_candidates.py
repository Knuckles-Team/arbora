"""Intent candidates: the capability candidate table, intent normalization and the resolution of one intent to candidates (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from scion_sdk.privacy import persistence_reference

from arbora.api.domain.fleet.lexical_floor import evidence
from arbora.api.domain.fleet.ranking import (
    FLOOR_ONLY,
    RankCandidate,
    Ranked,
    RankingPort,
)
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.intent_contract import (
    READ_ACTION_PREFIXES,
)
from arbora.mcp_server.backing.tool_specs import (
    INTENT_VERBS,
    READ_ONLY_ACTIONS,
    TOOL_VERBS,
)
from arbora.mcp_server.host_caches import HostCaches, IntentCache

logger = logging.getLogger(__name__)


#: Tools whose primary argument is a single free-text NL string — the resolver
#: seeds it from the caller's raw ``intent`` when the caller supplied no
#: structured ``hints`` for it, so ``ask("<plain English>")`` works with ZERO
#: hints (the specific UX program-design-2026-07-11 calls out). Every other
#: tool still resolves and dispatches — it just needs the caller's ``hints`` to
#: carry its real parameters, exactly as calling it directly would.
_PRIMARY_TEXT_PARAM: Mapping[str, str] = MappingProxyType(
    {
        "nl_query": "text",
        "ask_data": "question",
        "graph_ask": "question",
        "graph_search": "query",
        "graph_promql": "query",
        "graph_federated_search": "query",
        "graph_analyze": "query",
        "graph_code": "query",
        "graph_explain": "query",
        "graph_evaluate": "query",
    }
)


#: Safe universal fallback for ``ask`` when the top-ranked candidate needs
#: structured params the caller didn't supply — the engine's own NL planner
#: (CONCEPT:MERISTEM-KG.query.ask-gateway-rest-twin) always accepts raw text.
_ASK_FALLBACK_TOOL = "nl_query"


_DISPATCH_VERBS = frozenset({"ask", "write", "act", "manage", "why"})


_READ_ONLY_VERBS = frozenset({"ask", "why"})


_NON_READ_VERBS = frozenset({"write", "act", "manage"})


_AMBIGUITY_MARGIN = 0.05


_CALLER_OUTCOME_FIELDS = frozenset(
    {
        "calibrated_outcome_reward",
        "dispatch_outcome",
        "outcome_reward",
        "routing_feedback",
        "routing_reward",
    }
)


_CONTROL_HINT_FIELDS = frozenset({"tool", "_tool", "action", "plan_ref"})


#: Kept at the intent boundary rather than adding duplicate public parameters to
#: the underlying tool.  A delegation target is an ``agent_name`` regardless of
#: whether a caller describes it as an agent or its backing MCP server.
_DOCUMENTED_HINT_ALIASES: Mapping[str, dict[str, str]] = MappingProxyType({})


_DESTRUCTIVE_TERMS = frozenset(
    {
        "clear",
        "deactivate",
        "delete",
        "destroy",
        "drop",
        "evict",
        "invalidate",
        "prune",
        "purge",
        "remove",
        "revoke",
        "terminate",
        "uninstall",
        "unref",
        "wipe",
    }
)


_WORD_RE = re.compile(r"[a-z0-9]+")


@dataclass
class CapabilityCandidate:
    """One rankable arbora capability (a tool, or a tool+action pair)."""

    tool: str
    action: str | None
    verbs: tuple[str, ...]
    doc: str
    score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    #: Structural signature of a capability-DELEGATION façade (CONCEPT:MERISTEM-ECO.mcp.
    #: intent-surface-delegation-shape): its CPD declares a ``skill_name`` input
    #: parameter. Derived from the packaged CPD's ``typed_io.input_params``, not
    #: from the tool's literal name — any future delegation façade that accepts
    #: an exact ingested-skill name qualifies automatically, with zero new
    #: per-tool special-casing.
    accepts_skill_name: bool = False

    @property
    def capability_id(self) -> str:
        return f"{self.tool}:{self.action}" if self.action else self.tool


#: Soft weight of the learned reward-EMA blend into the lexical score (mirrors
#: ``CapabilityIndex.designate``'s own ``reward_weight`` default) — early on
#: (every candidate at the neutral 0.5 prior) the lexical ranking is
#: untouched; as outcomes accumulate a candidate's blended score rises or
#: sinks with its real success rate under that verb.
_LEARNED_REWARD_WEIGHT = 0.2


@dataclass(frozen=True)
class _PreviewPlan:
    """The minimum private state required to replay a reviewed intent plan."""

    created_at: float
    verb: str
    intent_ref: str
    outcome_scope_ref: str | None
    hints: dict[str, Any]


def _outcome_scope_ref() -> str | None:
    """Return an opaque verified authority partition for routing state.

    An absent ambient :class:`GraphSession` means there is no verified
    authority from which to derive a learning partition. Resolution remains
    available at the neutral prior, but no outcome may be learned.
    """

    from arbora.security.session import current_session

    session = current_session()
    if session is None:
        return None
    tenant_ref = persistence_reference("tenant", session.tenant)
    authority_policy = json.dumps(
        {
            "policy_version": str(session.policy_version),
            "audience": str(session.audience),
            "scopes": sorted(str(scope) for scope in session.scopes),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return persistence_reference(
        "intent_policy", authority_policy, namespace=tenant_ref
    )


def _reward_task_class(verb: str, scope_ref: str) -> str:
    return f"{scope_ref}:{verb}"


def _normalize_intent(intent: str) -> str:
    """Whitespace/case-fold an intent string for resolution-cache keying."""
    return " ".join(str(intent or "").strip().lower().split())


def _cache_key(
    verb: str | None,
    intent: str,
    hints: dict[str, Any],
    top_k: int,
    outcome_scope_ref: str | None,
    generation: int,
) -> tuple[Any, ...]:
    """The resolution-cache key (CONCEPT:MERISTEM-ECO.mcp.intent-surface-resolution-cache).

    Intent and hints are represented by non-reversible references so the LRU
    never retains prompt text, credentials, personal data, endpoints, or local
    paths. The opaque tenant+policy partition prevents a cached ranking from
    crossing an authorization-policy boundary.
    """
    normalized = _normalize_intent(intent)
    hints_json = json.dumps(
        hints or {}, sort_keys=True, default=str, separators=(",", ":")
    )
    return (
        verb,
        persistence_reference("intent", normalized),
        persistence_reference("intent_hints", hints_json),
        outcome_scope_ref or "unverified",
        int(top_k),
        generation,
    )


_ARBORA_CPDS_PATH = Path(__file__).resolve().parent.parent / "arbora_capabilities.json"


@functools.cache
def _arbora_cpds() -> dict[str, dict[str, Any]]:
    """Capability descriptors for every operation arbora serves."""
    raw = json.loads(_ARBORA_CPDS_PATH.read_text(encoding="utf-8"))
    return {entry["id"]: entry for entry in raw.get("capabilities", [])}


def _load_cpds_required() -> dict[str, dict[str, Any]]:
    """Return the packaged current CPD authority or fail closed."""

    cpds = _arbora_cpds()
    if not cpds:
        raise RuntimeError("Arbora capability descriptors are unavailable")
    return cpds


@functools.cache
def _actions_by_tool() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    from arbora.mcp_server.backing._arbora_action_manifest import ARBORA_ACTIONS

    for op in ARBORA_ACTIONS:
        action = op.get("action")
        if action is None:
            continue
        out.setdefault(op["tool"], []).append(action)
    return out


def _build_candidates(
    caches: HostCaches, *, force: bool = False
) -> list[CapabilityCandidate]:
    """Build one CPD-backed candidate per live granular tool.

    CONCEPT:MERISTEM-ECO.mcp.intent-surface-cpd-ranking. The packaged CPD set is
    mandatory: a registered tool without a descriptor fails closed instead of
    inheriting a guessed verb or effect policy. Candidate text comes from its
    current ``one_line``/``examples``/``does`` descriptor fields.

    Cached process-wide (tool registration happens once at server build); pass
    ``force=True`` in tests after changing the host tool table. Bumps
    the candidate generation on every actual rebuild (CONCEPT:MERISTEM-ECO.mcp.intent-surface-resolution-cache).
    """
    built = caches.intent.candidates()
    if built is not None and not force:
        return built
    runtime.ensure_tools_registered(caches)
    actions_by_tool = _actions_by_tool()
    cpds = _load_cpds_required()
    live_tools = sorted(set(caches.tools) - set(INTENT_VERBS))
    _require_cpd_coverage(live_tools, cpds)
    _require_verb_authority_coverage(live_tools)

    # Intent verbs have CPDs because they are first-class MCP/REST entry points,
    # but they can never be resolver targets: selecting ``ask`` from inside
    # ``ask`` would recursively dispatch intent routing instead of a capability.
    out = [
        _build_candidate_for_tool(tool, cpds[tool], actions_by_tool)
        for tool in live_tools
    ]
    caches.intent.store_candidates(out)
    return out


def _require_cpd_coverage(
    live_tools: list[str], cpds: dict[str, dict[str, Any]]
) -> None:
    """Helper for `_build_candidates`: fail closed on a registered tool without a CPD."""
    missing_cpds = sorted(set(live_tools) - set(cpds))
    if missing_cpds:
        raise RuntimeError(
            "Arbora capability descriptors are missing for registered tools: "
            + ", ".join(missing_cpds)
        )


def _require_verb_authority_coverage(live_tools: list[str]) -> None:
    """Helper for `_build_candidates`: fail closed on a tool without verb authority."""
    missing_authority = sorted(set(live_tools) - set(TOOL_VERBS))
    if missing_authority:
        raise RuntimeError(
            "Arbora intent-verb authority is missing registered tools: "
            + ", ".join(missing_authority)
        )


def _validate_tool_authority_verbs(tool: str, authority_verbs: tuple[str, ...]) -> None:
    """Helper for `_build_candidates`: authority_verbs must be a non-empty, unique subset of INTENT_VERBS."""
    if (
        not authority_verbs
        or len(set(authority_verbs)) != len(authority_verbs)
        or not set(authority_verbs) <= set(INTENT_VERBS)
    ):
        raise RuntimeError(
            f"Arbora intent-verb authority is invalid for {tool}: {authority_verbs!r}"
        )


def _validate_packaged_verbs(
    tool: str, cpd: dict[str, Any], authority_verbs: tuple[str, ...]
) -> None:
    """Helper for `_build_candidates`: packaged CPD intent_verbs must match TOOL_VERBS exactly."""
    packaged_verbs = cpd.get("intent_verbs")
    if not isinstance(packaged_verbs, list) or not all(
        isinstance(verb, str) for verb in packaged_verbs
    ):
        raise RuntimeError(
            f"Arbora capability descriptor has an invalid intent_verbs field for {tool}"
        )
    if tuple(packaged_verbs) != authority_verbs:
        raise RuntimeError(
            "Arbora capability descriptor intent-verb drift for "
            f"{tool}: expected {list(authority_verbs)!r}, packaged {packaged_verbs!r}"
        )


def _build_candidate_for_tool(
    tool: str,
    cpd: dict[str, Any],
    actions_by_tool: dict[str, list[str]],
) -> CapabilityCandidate:
    """Helper for `_build_candidates`: validate one tool's CPD and build its candidate."""
    authority_verbs = TOOL_VERBS[tool]
    _validate_tool_authority_verbs(tool, authority_verbs)
    _validate_packaged_verbs(tool, cpd, authority_verbs)

    examples_text = " ".join(str(e) for e in (cpd.get("examples") or ()))
    does_text = " ".join(str(d.get("action", "")) for d in (cpd.get("does") or ()))
    doc = (
        f"{tool} {' '.join(actions_by_tool.get(tool, []))} "
        f"{cpd.get('one_line', '')} {examples_text} {does_text}"
    )
    input_params = (cpd.get("typed_io") or {}).get("input_params") or ()
    accepts_skill_name = any(
        isinstance(p, dict) and p.get("name") == "skill_name" for p in input_params
    )
    return CapabilityCandidate(
        tool=tool,
        action=None,
        verbs=authority_verbs,
        doc=doc,
        accepts_skill_name=accepts_skill_name,
    )


def _declared_phrases(tool: str) -> str:
    """The tool's declared multi-word action phrases, as candidate text."""

    return " ".join(
        " ".join(_WORD_RE.findall(action.lower()))
        for action in _actions_by_tool().get(tool, ())
        if len(_WORD_RE.findall(action.lower())) >= 2
    )


def _port_text(c: CapabilityCandidate) -> str:
    """One capability's ranking text: its doc, declared verbs and action phrases.

    The skill-delegation case is text too: a façade that accepts a named skill says
    so, and the port, not a bonus, decides whether the request names one
    (ARBORA-FLEET-R083.2).
    """
    parts = [c.doc, " ".join(c.verbs), _declared_phrases(c.tool)]
    if c.accepts_skill_name:
        parts.append("delegate to a named skill")
    return " ".join(part for part in parts if part)


def resolve_intent(
    caches: HostCaches,
    verb: str | None,
    intent: str,
    *,
    hints: dict[str, Any] | None = None,
    top_k: int = 5,
    ranking: RankingPort = FLOOR_ONLY,
) -> list[CapabilityCandidate]:
    """Rank candidate capabilities for ``intent`` under ``verb`` (``None`` = all verbs).

    An explicit ``hints["tool"]`` (or ``hints["_tool"]``) pins resolution only
    when that tool is authorized for the requested verb. A pin can remove
    ranking ambiguity; it cannot elevate ``ask`` into a mutation policy.

    A ranked (non-pinned) resolution is served from the bounded resolution
    cache (CONCEPT:MERISTEM-ECO.mcp.intent-surface-resolution-cache) when the SAME
    ``(verb, normalized intent, hints, top_k)`` was already resolved under the
    CURRENT routing policy (candidate-table generation + reward epoch
    unchanged); otherwise it re-ranks, blending each candidate's learned
    outcome reward-EMA (CONCEPT:MERISTEM-ECO.mcp.intent-surface-outcome-learning) into its lexical score
    before caching the result.
    """
    if verb is not None and verb not in INTENT_VERBS:
        return []
    top_k = max(1, min(int(top_k), 20))
    hints = hints or {}
    pinned = hints.get("tool") or hints.get("_tool")
    candidates = _build_candidates(caches)
    if pinned:
        return _pinned_resolution(candidates, verb, pinned, hints)

    outcome_scope_ref = _outcome_scope_ref()
    cache_key = _cache_key(
        verb, intent, hints, top_k, outcome_scope_ref, caches.intent.generation
    )
    cached = caches.intent.resolution(cache_key)
    if cached is not None:
        return cached

    explicit_action = hints.get("action")
    pool = _resolution_pool(candidates, verb, explicit_action)
    cpds = _load_cpds_required() if explicit_action is not None else {}
    request = _PortRequest(intent, explicit_action, cpds, ranking, top_k)
    return _resolve_by_port(caches.intent, cache_key, pool, request)


@dataclass(frozen=True)
class _PortRequest:
    """One resolution's inputs to the ranking port."""

    intent: str
    explicit_action: Any
    cpds: dict[str, dict[str, Any]]
    ranking: RankingPort
    top_k: int


def _resolve_by_port(
    intent_cache: IntentCache,
    cache_key: tuple[Any, ...],
    pool: list[CapabilityCandidate],
    request: _PortRequest,
) -> list[CapabilityCandidate]:
    """The pool in the port's order with the floor's evidence, cached when final.

    An engine order is final; a floor answer is final only when no engine is bound,
    so a recovered engine is asked again rather than served a degraded order.
    """
    rows = [_pool_row(c, request.explicit_action, request.cpds) for c in pool]
    ordered = _ordered(
        request.ranking, "catalog.intent_action_rank", request.intent, rows
    )
    by_id = {row.id: c for row, c in zip(rows, pool, strict=True)}
    shown = evidence(request.intent, ((r.id, r.name, r.description) for r in rows))
    result = [
        _resolved_candidate(by_id[i], request.explicit_action, *shown[i])
        for i in ordered.order[: request.top_k]
    ]
    final = ordered.source == "medulla" or not getattr(
        request.ranking, "configured", False
    )
    intent_cache.store_resolution(cache_key, result) if final else None
    return list(result)


def _pool_row(
    c: CapabilityCandidate, explicit_action: Any, cpds: dict[str, dict[str, Any]]
) -> RankCandidate:
    """One capability as the port sees it: tool, action, one-line description, verbs."""
    if explicit_action is not None:
        one_line = cpds[c.tool].get("one_line", "")
        return RankCandidate(
            c.capability_id, c.tool, f"{c.tool} {explicit_action} {one_line}", "action"
        )
    return RankCandidate(c.capability_id, c.tool, _port_text(c), "action")


def _ordered(
    port: RankingPort, question: str, intent: str, rows: list[RankCandidate]
) -> Ranked:
    ordered = getattr(port, "ordered", None)
    if ordered is not None:
        return ordered(question, intent, rows)
    return Ranked(port.rank(question, intent, rows), "medulla")


def _resolved_candidate(
    c: CapabilityCandidate, explicit_action: Any, relevance: float, matched: list[str]
) -> CapabilityCandidate:
    """The capability as resolved: the port's pick with the floor's evidence."""
    return CapabilityCandidate(
        tool=c.tool,
        action=str(explicit_action) if explicit_action is not None else c.action,
        verbs=c.verbs,
        doc=c.doc,
        score=relevance,
        matched_terms=matched,
        accepts_skill_name=c.accepts_skill_name,
    )


def _pinned_resolution(
    candidates: list[CapabilityCandidate],
    verb: str | None,
    pinned: str,
    hints: dict[str, Any],
) -> list[CapabilityCandidate]:
    """Helper for `resolve_intent`: resolve a hints["tool"]-pinned request.

    Empty list if `pinned` names no candidate authorized for `verb`. ``act``
    is authorized for an operation none of the tool's declared verbs can
    classify (:func:`_act_fallback`); it previews and executes it as a mutation.
    """
    action = hints.get("action")
    for c in candidates:
        authorized = (
            verb is None
            or verb in c.verbs
            or (verb == "act" and _act_fallback(c.tool, action or c.action))
        )
        if c.tool == pinned and authorized:
            return [
                CapabilityCandidate(
                    tool=c.tool,
                    action=hints.get("action") or c.action,
                    verbs=c.verbs,
                    doc=c.doc,
                    score=1.0,
                    matched_terms=["explicit tool hint"],
                )
            ]
    return []


def _resolution_pool(
    candidates: list[CapabilityCandidate],
    verb: str | None,
    explicit_action: Any,
) -> list[CapabilityCandidate]:
    """Helper for `resolve_intent`: filter candidates by verb, then by explicit action."""
    pool = candidates if verb is None else [c for c in candidates if verb in c.verbs]
    if explicit_action is not None:
        actions_by_tool = _actions_by_tool()
        pool = [
            candidate
            for candidate in pool
            if explicit_action in actions_by_tool.get(candidate.tool, ())
        ]
    return pool


def _order_actions(
    tool: str, intent: str, ranking: RankingPort = FLOOR_ONLY
) -> list[tuple[str, float]]:
    """The tool's actions in the port's order, each with the floor's evidence."""

    actions = _actions_by_tool().get(tool)
    if not actions:
        return []
    rows = [
        RankCandidate(action, action, action.replace("_", " "), "action")
        for action in actions
    ]
    ordered = _ordered(ranking, "catalog.intent_action_rank", intent, rows)
    shown = evidence(intent, ((r.id, r.name, r.description) for r in rows))
    return [(action, shown[action][0]) for action in ordered.order]


def _literal_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    return None


def _operation_record(cpd: dict[str, Any], action: str | None) -> dict[str, Any] | None:
    operations = [op for op in cpd.get("does", ()) if isinstance(op, dict)]
    if action is not None:
        return next((op for op in operations if op.get("action") == action), None)
    if len(operations) == 1:
        return operations[0]
    return None


def _operation_is_destructive(tool: str, action: str | None) -> bool:
    words = set(_WORD_RE.findall(f"{tool} {action or ''}".lower()))
    return not words.isdisjoint(_DESTRUCTIVE_TERMS)


#: Action-name prefixes treated as read-only when nothing more authoritative
#: (a declared ``mutates``, the destructive-terms check, or the reviewed
#: READ_ONLY_ACTIONS allowlist) has already classified the operation. Used
#: only by :func:`_resolve_mutates`.
_READ_ACTION_PREFIXES = READ_ACTION_PREFIXES


def _mutates_from_declaration(
    declared_mutation: bool | None, destructive: bool
) -> bool | None:
    """Helper for `_resolve_mutates`: declared/destructive authority (None = inconclusive)."""
    if declared_mutation is not None:
        return declared_mutation
    if destructive:
        return True
    return None


def _mutates_from_action_policy(
    tool: str, action: str | None, action_is_declared_read: bool
) -> bool | None:
    """Helper for `_resolve_mutates`: the READ_ONLY_ACTIONS allowlist policy."""
    if action_is_declared_read:
        return False
    if action is not None and tool in READ_ONLY_ACTIONS:
        # The reviewed allowlist is the action policy for a mixed surface:
        # anything not explicitly declared read-only is non-read.
        return True
    return None


def _mutates_from_verb_and_prefix(
    verb: str, action: str | None, action_prefix: str
) -> bool | None:
    """Helper for `_resolve_mutates`: name/verb-based inference fallback."""
    if action_prefix in _READ_ACTION_PREFIXES:
        return False
    if action is None and verb in _READ_ONLY_VERBS:
        return False
    if verb in _NON_READ_VERBS:
        return True
    return None


def _resolve_mutates(
    verb: str,
    tool: str,
    action: str | None,
    declared_mutation: bool | None,
    destructive: bool,
) -> bool | None:
    """Helper for `_operation_plan`: resolve the operation's mutation classification.

    Tries, in priority order: declared/destructive authority, the
    READ_ONLY_ACTIONS allowlist policy, then name/verb-based inference.
    """
    result = _mutates_from_declaration(declared_mutation, destructive)
    if result is not None:
        return result
    action_is_declared_read = action in READ_ONLY_ACTIONS.get(tool, frozenset())
    result = _mutates_from_action_policy(tool, action, action_is_declared_read)
    if result is not None:
        return result
    action_prefix = str(action or "").split("_", 1)[0]
    return _mutates_from_verb_and_prefix(verb, action, action_prefix)


def _execution_class_and_impact(
    destructive: bool, mutates: bool | None
) -> tuple[str, str]:
    """Helper for `_operation_plan`: (execution_class, impact_summary)."""
    if destructive:
        return "destructive", "May remove or irreversibly invalidate governed state."
    if mutates is True:
        return (
            "mutation",
            "Changes governed state within the selected capability scope.",
        )
    if mutates is False:
        return "read_only", "Reads or computes without a declared state mutation."
    return "unclassified", "Effect metadata is insufficient; execution fails closed."


def _approval_info(cpd: dict[str, Any], destructive: bool) -> dict[str, Any]:
    """Helper for `_operation_plan`: the ``approval`` block."""
    raw_policy = cpd.get("policy")
    policy = raw_policy if isinstance(raw_policy, dict) else {}
    approval_class = str(policy.get("approval_class") or "unclassified")
    approval_required = destructive or approval_class != "auto"
    return {
        "class": approval_class,
        "required": approval_required,
        "route": "exact_tool" if approval_required else "intent_policy",
    }


def _declared_flag(operation: dict[str, Any] | None, key: str) -> bool | None:
    """Helper for `_operation_plan`: a declared boolean CPD operation flag, or None."""
    return _literal_bool(operation.get(key) if operation is not None else None)


def _operation_cost_latency(cpd: dict[str, Any]) -> dict[str, Any]:
    """Helper for `_operation_metadata`: cost/latency, with fallbacks applied."""
    cost = cpd.get("cost") if isinstance(cpd.get("cost"), dict) else {}
    latency = cpd.get("latency") if isinstance(cpd.get("latency"), dict) else {}
    return {
        "cost": cost or {"estimate": "not_available"},
        "latency": latency or {"estimate": "not_available"},
    }


def _operation_impact_fields(
    cpd: dict[str, Any], operation: dict[str, Any] | None
) -> dict[str, Any]:
    """Helper for `_operation_metadata`: scopes/durability/transaction, with fallbacks applied."""
    scopes = sorted(str(scope) for scope in (cpd.get("scopes") or ()))
    durability = str(operation.get("durability") or "") if operation is not None else ""
    transaction = (
        str(operation.get("txn_participation") or "") if operation is not None else ""
    )
    return {
        "scopes": scopes,
        "durability": durability or "unclassified",
        "transaction": transaction or "unclassified",
    }


def _operation_metadata(
    cpd: dict[str, Any], operation: dict[str, Any] | None
) -> dict[str, Any]:
    """Helper for `_operation_plan`: cost/latency/scopes/durability/transaction, fallbacks applied."""
    return {
        **_operation_cost_latency(cpd),
        **_operation_impact_fields(cpd, operation),
    }


def _operation_plan(
    verb: str,
    tool: str,
    action: str | None,
    call_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Build a value-free preview from the current CPD operation authority."""

    cpd = _load_cpds_required()[tool]
    operation = _operation_record(cpd, action)
    declared_mutation = _declared_flag(operation, "mutates")
    destructive = _operation_is_destructive(tool, action)
    mutates = _resolve_mutates(verb, tool, action, declared_mutation, destructive)

    declared_idempotency = _declared_flag(operation, "idempotent")
    if declared_idempotency is None and mutates is False:
        declared_idempotency = True

    metadata = _operation_metadata(cpd, operation)
    execution_class, impact_summary = _execution_class_and_impact(destructive, mutates)

    return {
        "tool": tool,
        "action": action,
        "execution_class": execution_class,
        "mutates": mutates,
        "destructive": destructive,
        "idempotent": declared_idempotency,
        "preview_required": verb in _NON_READ_VERBS,
        "approval": _approval_info(cpd, destructive),
        "impact": {
            "summary": impact_summary,
            "scopes": metadata["scopes"],
            "durability": metadata["durability"],
            "transaction": metadata["transaction"],
        },
        "cost": metadata["cost"],
        "latency": metadata["latency"],
        "forwarded_fields": sorted(call_kwargs),
    }


def _policy_verbs(tool: str, action: str | None) -> tuple[str, ...]:
    """The dispatch verbs whose declared policy classifies this operation."""
    verbs: list[str] = []
    for verb in TOOL_VERBS.get(tool, ()):
        if verb not in _DISPATCH_VERBS:
            continue
        plan = _operation_plan(verb, tool, action, {})
        if plan["execution_class"] == "unclassified":
            continue
        if verb in _READ_ONLY_VERBS and plan["mutates"] is not False:
            continue
        verbs.append(verb)
    return tuple(verbs)


def _act_fallback(tool: str, action: str | None) -> bool:
    """Whether ``act`` serves ``tool``'s ``action`` as an unclassified operation."""
    return "act" not in TOOL_VERBS.get(tool, ()) and not _policy_verbs(tool, action)


def _port_of(mcp: Any) -> RankingPort:
    """The host's ranking port, reached through the attached fleet multiplexer."""
    fleet = getattr(getattr(mcp, "_fleet_mux", None), "fleet", None)
    return getattr(fleet, "ranking", FLOOR_ONLY)
