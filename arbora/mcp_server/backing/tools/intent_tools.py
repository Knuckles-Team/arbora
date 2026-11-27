"""The arbora intent surface (CONCEPT:MERISTEM-ECO.mcp.intent-surface-condensed-collapse).

Six intent verbs (``ask``/``find``/``write``/``act``/``manage``/``why``) are
the default model-facing Arbora API. The packaged Capability Power Descriptor
(CPD) set is the required routing and operation-safety authority. Missing CPDs
fail closed so a newly registered capability cannot silently bypass verb,
scope, effect, or approval classification.

Dispatch is proof-carrying. Every response includes the ranked evidence,
effective operation, policy classification, preview plan, and result
provenance. ``ask`` is read-only; a tool pin cannot change that policy.
Non-read verbs preview by default, ambiguous mutations do not execute, and
an approval-required (destructive, or a non-``auto`` ``approval_class``)
operation only executes after the caller's session approved that exact
operation through the separately governed ``manage(action="approve")``
(BUG-040 — see :func:`_operation_approved`); a tool pin never bypasses it.

The six verbs are arbora's whole MCP surface: each takes the ecosystem's
condensed contract (:mod:`arbora.mcp_server.backing.intent_contract`) — ``action``
(an operation id of the generated manifest, or ``describe``), ``params``,
optional natural-language ``intent`` and ``execute``. ``find`` also reaches the
fleet catalog and ``act`` also calls fleet tools — directly by
``action='<server>.<tool>'`` (MERISTEM-CONTROL-R032) or indirectly via
``action='fleet.call'`` — and the host's native operations, so no separate
fleet meta-tool exists.

Outcome learning accepts only the observed result of an unpinned,
unambiguous, policy-authorized execution. Caller-supplied feedback is rejected.
Both reward keys and resolution-cache keys are partitioned by an opaque digest
of the verified tenant and policy revision, preventing cross-tenant or stale-
policy influence. The shared :class:`OutcomeRouter` remains the sole reward-EMA
mechanism; no second learner is introduced.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Mapping, Sequence
from functools import partial
from types import MappingProxyType
from typing import Any

from scion_sdk.privacy import persistence_reference

from arbora.api.domain.fleet.lexical_floor import evidence
from arbora.api.domain.fleet.ranking import (
    FLOOR_ONLY,
    RankCandidate,
    RankingPort,
    run_with_port,
)
from arbora.api.errors import not_served
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.intent_contract import (
    DESCRIBE_ACTION,
    operation_id,
)
from arbora.mcp_server.backing.optional_tool_features import OPTIONAL_TOOL_FEATURES
from arbora.mcp_server.backing.tool_specs import (
    INTENT_VERBS,
    TOOL_VERBS,
)
from arbora.mcp_server.backing.tools.intent_candidates import (
    _DISPATCH_VERBS,
    _READ_ONLY_VERBS,
    CapabilityCandidate,
    _actions_by_tool,
    _load_cpds_required,
    _operation_record,
    _ordered,
    _outcome_scope_ref,
    _policy_verbs,
    _port_of,
    _port_text,
    resolve_intent,
)
from arbora.mcp_server.backing.tools.intent_dispatch import (
    dispatch_intent,
)
from arbora.mcp_server.backing.tools.intent_policy import (
    _intent_security_failure,
    _plan_ref,
    _record_approval,
)
from arbora.mcp_server.host_caches import HostCaches, IntentCache, ToolTable

logger = logging.getLogger(__name__)

__all__ = [
    "INTENT_VERBS",
    "TOOL_VERBS",
    "CapabilityCandidate",
    "resolve_intent",
    "dispatch_intent",
    "register_intent_tools",
]


#: Session-scoped approvals recorded by ``manage(action="approve")`` live in the
#: host's :class:`~arbora.mcp_server.host_caches.IntentCache`, bounded and expiring
#: like the preview plans; a restart or expiry requires a fresh approval.


#: ``act`` operation that calls one tool of another fleet MCP server.
FLEET_CALL_ACTION = "fleet.call"
#: ``manage`` operations that mount fleet servers/tools ahead of use and
#: release them again (``fleet.call`` mounts on demand either way).
FLEET_LOAD_ACTION = "fleet.load"
FLEET_UNLOAD_ACTION = "fleet.unload"
#: ``manage`` operation that approves one approval-required operation for the
#: caller's session (see :func:`_operation_approved`).
APPROVE_ACTION = "approve"
#: ``manage``'s read-only lakehouse maintenance status operation.
LAKEHOUSE_STATUS_ACTION = "lakehouse_status"
#: ``find``'s fixed action set: capability search (default), fleet tool search,
#: the fleet catalog, fleet health, and operation descriptions.
FIND_ACTIONS = ("capabilities", "tools", "catalog", "status", DESCRIBE_ACTION)
_FIND_TOP_K = 8
#: Request bounds on fleet operations (the retired load_tools/unload_tools
#: meta-tools' safety boundary).
_FLEET_LIST_LIMITS = MappingProxyType({"tools": 128, "servers": 32})

#: Operations a verb serves beside the generated manifest.
_HOST_ACTIONS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "find": FIND_ACTIONS,
        "act": (FLEET_CALL_ACTION,),
        "manage": (
            APPROVE_ACTION,
            FLEET_LOAD_ACTION,
            FLEET_UNLOAD_ACTION,
            LAKEHOUSE_STATUS_ACTION,
        ),
    }
)

_VERB_SUMMARIES = MappingProxyType(
    {
        "ask": "Read or answer from the Knowledge Graph.",
        "find": "Discover capabilities, operations, fleet tools and the fleet catalog.",
        "write": "Create or change graph data; previews first, then execute with plan_ref.",
        "act": "Run work, an operation or a fleet tool; previews first, then execute with plan_ref.",
        "manage": "Configure and govern arbora and fleet loading; previews first, then execute with plan_ref.",
        "why": "Explain beliefs, decisions, provenance and changes.",
    }
)


def _verb_description(verb: str) -> str:
    if verb == "find":
        return (
            f"{_VERB_SUMMARIES[verb]} Default: rank capabilities for `intent`; "
            "'describe' explains an operation."
        )
    return (
        f"{_VERB_SUMMARIES[verb]} action='<tool>.<op>' (see action='describe'), "
        "or leave it empty to route `intent`."
    )


def _accepting_verbs(tool: str, action: str | None) -> tuple[str, ...]:
    """The intent verbs whose router policy accepts this exact operation.

    An operation none of its declared verbs can classify (no declared effect,
    not on a reviewed read-only allowlist) is reachable through ``act`` alone,
    which treats it as a mutation: preview, ``plan_ref`` and the CPD approval
    policy all apply (see :func:`_act_fallback`).
    """
    return _policy_verbs(tool, action) or ("act",)


@functools.cache
def operation_verbs() -> dict[str, tuple[str, ...]]:
    """``{operation id: verbs that accept it}`` for every manifest operation."""
    from arbora.mcp_server.backing.arbora_surface import manifest_operations

    return {
        op_id: _accepting_verbs(tool, action)
        for op_id, (tool, action) in manifest_operations().items()
    }


def _verb_catalog(mcp: Any, verb: str, group: str | None) -> dict[str, Any]:
    """Operation ids ``verb`` accepts, grouped by routing group."""
    from arbora.mcp_server.backing.arbora_surface import (
        group_for_tool,
        host_operations,
        manifest_operations,
    )

    table = manifest_operations()
    grouped: dict[str, list[str]] = {}
    for op_id, verbs in operation_verbs().items():
        name = group_for_tool(table[op_id][0]) or ""
        if verb in verbs and (group is None or name == group):
            grouped.setdefault(name, []).append(op_id)
    extra = list(_HOST_ACTIONS.get(verb, ()))
    if verb == "act":
        extra += sorted(host_operations(mcp))
    if extra and group is None:
        grouped["host"] = extra
    return {"verb": verb, "operations": grouped}


def _describe_host_operation(mcp: Any, op_id: str) -> dict[str, Any] | None:
    """``describe`` for a host-native backing operation, or ``None``."""
    from arbora.mcp_server.backing.arbora_surface import (
        backing_tools,
        host_operations,
    )
    from arbora.mcp_server.backing.intent_contract import schema_without_action

    resolved = host_operations(mcp).get(op_id)
    if resolved is None:
        return None
    tool = backing_tools(mcp)[resolved[0]]
    return {
        "action": op_id,
        "verbs": ["act"],
        "description": getattr(tool, "description", None) or "",
        "params_schema": schema_without_action(tool.parameters or {}),
    }


def _describe_operation(mcp: Any, op_id: str) -> dict[str, Any]:
    """One operation: backing tool, accepting verbs, description, argument schema."""
    from arbora.mcp_server.backing.arbora_surface import (
        backing_tools,
        group_for_tool,
        resolve_operation,
    )
    from arbora.mcp_server.backing.intent_contract import schema_without_action

    resolved = resolve_operation(op_id)
    if resolved is None:
        host = _describe_host_operation(mcp, op_id)
        if host is not None:
            return host
        return {"error": f"Unknown operation {op_id!r}; action='describe' lists them."}
    tool_name, action = resolved
    tool = backing_tools(mcp).get(tool_name)
    cpd = _load_cpds_required().get(tool_name) or {}
    record = _operation_record(cpd, action) or {}
    return {
        "action": op_id,
        "group": group_for_tool(tool_name),
        "verbs": list(operation_verbs().get(op_id, ())),
        "description": str(record.get("description") or cpd.get("one_line") or ""),
        "params_schema": schema_without_action(getattr(tool, "parameters", None) or {}),
        "available": tool is not None,
    }


def _describe(mcp: Any, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    target = params.get("action")
    if target:
        return _describe_operation(mcp, str(target))
    group = params.get("group")
    return _verb_catalog(mcp, verb, str(group) if group else None)


def _operation_hints(
    tools: ToolTable, tool: str, action: str | None, params: dict[str, Any]
) -> dict[str, Any]:
    """Router hints for an explicit operation: shaped arguments + the pin."""
    from arbora.mcp_server.backing.intent_contract import shape_arguments

    controls = {key: params.pop(key) for key in ("plan_ref",) if key in params}
    function = tools.get(tool)
    accepted: set[str] = set()
    var_keyword = False
    if function is not None:
        parameters = inspect.signature(function).parameters
        accepted = set(parameters)
        var_keyword = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        )
    hints = shape_arguments(accepted, params, action, accepts_var_keyword=var_keyword)
    hints.update(controls)
    hints["tool"] = tool
    return hints


def _fleet_mux(mcp: Any) -> Any:
    """The attached fleet multiplexer, or a precise refusal (MERISTEM-CONTROL-R031).

    ``runtime.ensure_tools_registered``'s headless build records the exact
    attach failure on ``mcp._fleet_mux_unavailable_reason`` when it could not
    attach a real multiplexer; surface that verbatim instead of the generic
    embedded/headless message so an operator sees why, not just that.
    """
    mux = getattr(mcp, "_fleet_mux", None)
    if mux is not None:
        return mux
    reason = getattr(mcp, "_fleet_mux_unavailable_reason", None)
    if reason:
        raise ValueError(f"No fleet multiplexer is attached to this server: {reason}.")
    raise ValueError(
        "No fleet multiplexer is attached to this server (embedded/headless build)."
    )


def _host_plan_ref(verb: str, op_id: str, arguments: dict[str, Any]) -> str:
    """Bind a host/fleet operation preview to its verb, operation and arguments."""
    return _plan_ref(verb, "host", op_id, arguments, _outcome_scope_ref())


async def _governed(
    verb: str,
    op_id: str,
    arguments: dict[str, Any],
    params: dict[str, Any],
    execute: bool,
    run: Any,
) -> dict[str, Any]:
    """Preview → ``plan_ref`` → execute for an operation outside the manifest.

    The same contract :func:`dispatch_intent` enforces for non-read verbs: a
    call without ``execute`` returns the plan and its ``plan_ref``; execution
    requires resubmitting that ``plan_ref`` for the identical operation and
    arguments.
    """
    plan_ref = _host_plan_ref(verb, op_id, arguments)
    plan = {
        "action": op_id,
        "arguments": sorted(arguments),
        "preview_required": True,
        "plan_ref": plan_ref,
    }
    if not execute:
        return {"executed": False, "plan": plan}
    if str(params.get("plan_ref") or "") != plan_ref:
        return {
            "executed": False,
            "plan": plan,
            "error": (
                "Preview required: call with execute=false, review the plan, then "
                "resubmit its plan_ref with execute=true."
            ),
        }
    return {"executed": True, "action": op_id, "result": await run()}


def _bounded_names(params: dict[str, Any], key: str) -> list[str]:
    names = params.get(key) or []
    if not isinstance(names, list) or len(names) > _FLEET_LIST_LIMITS[key]:
        raise ValueError(
            f"params.{key} must be a list of at most {_FLEET_LIST_LIMITS[key]} names"
        )
    return [str(name) for name in names]


async def _find(
    caches: HostCaches, mcp: Any, action: str, params: dict[str, Any], intent: str
) -> Any:
    """``find``: capability ranking, fleet tool search, fleet catalog, fleet health."""
    selected = action or "capabilities"
    if selected not in FIND_ACTIONS:
        return {
            "error": f"Unknown find action {action!r}.",
            "actions": list(FIND_ACTIONS),
        }
    top_k = max(1, min(int(params.get("top_k") or _FIND_TOP_K), 100))
    if selected == "capabilities":
        if not intent:
            return {"error": "find needs an `intent` to rank capabilities."}
        return await _find_capability(caches, mcp, intent, top_k=top_k)
    mux = _fleet_mux(mcp)
    mux.require_capability("discover")
    if selected == "tools":
        query = intent or str(params.get("query") or "")
        return await mux.discover_tools(query, top_k=top_k, loaded=set())
    if selected == "catalog":
        return await mux.list_catalog(
            server=str(params.get("server") or "")[:128],
            include_tools=bool(params.get("include_tools", True)),
        )
    return mux.status_snapshot()


async def _fleet_call(mcp: Any, params: dict[str, Any], execute: bool) -> Any:
    """``act(action="fleet.call")``: call one fleet tool by its catalog name."""
    tool = str(params.get("tool") or "")
    arguments = params.get("arguments") or {}
    if not tool or not isinstance(arguments, dict):
        return {
            "error": "fleet.call needs params {'tool': '<fleet tool>', 'arguments': {...}}."
        }

    async def _run() -> Any:
        mux = _fleet_mux(mcp)
        mux.require_capability("delegate")
        await mux.resolve_and_mount(tools=[tool], servers=None)
        if mux._authority_scope is None:
            result = await mux.call_proxied_tool(tool, arguments)
        else:
            with mux._authority_scope():
                result = await mux.call_proxied_tool(tool, arguments)
        return result.model_dump(mode="json", by_alias=True, exclude_none=True)

    return await _governed(
        "act", FLEET_CALL_ACTION, {"tool": tool, **arguments}, params, execute, _run
    )


def _fleet_dotted_tool_name(mcp: Any, action: str) -> str | None:
    """Resolve ``act(action='<server>.<tool>')`` to its fleet prefixed tool name
    (MERISTEM-CONTROL-R032), or ``None`` when no catalog server matches the prefix.

    The multiplexer's own internal join is ``<prefix>__<cleaned tool name>``
    (:func:`~arbora.fleet.multiplexer.clean_tool_name`); this is the
    single, deterministic translation from the dotted ``<server>.<tool>``
    action id a caller types into that exact internal name, reusing the
    multiplexer's own collision-free prefix/catalog lookup
    (``server_for_prefixed``) — never a second guess at server naming. A dot
    with no matching catalog server (a typo, or an unrelated dotted action
    id) returns ``None`` so the caller falls through to the native manifest's
    own "Unknown operation" error instead of a confusing fleet failure.
    """
    mux = getattr(mcp, "_fleet_mux", None)
    if mux is None or "." not in action:
        return None
    prefixed = action.replace(".", "__", 1)
    return prefixed if mux.mounts.server_for_prefixed(prefixed) is not None else None


def _fleet_dispatch_params(tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Build ``_fleet_call``'s ``{tool, arguments}`` params shape for a direct
    ``act(action='<server>.<tool>')`` dispatch (MERISTEM-CONTROL-R032).

    Mirrors native-operation ergonomics: ``params`` IS the argument dict
    directly (no caller-visible ``arguments`` nesting), exactly like
    ``act(action='<tool>.<op>', params={...})`` already works for a manifest
    operation. ``plan_ref`` stays a top-level control field so the existing
    preview → ``plan_ref`` → execute contract :func:`_governed` enforces is
    unaffected.
    """
    arguments = {k: v for k, v in params.items() if k != "plan_ref"}
    call_params: dict[str, Any] = {"tool": tool, "arguments": arguments}
    if "plan_ref" in params:
        call_params["plan_ref"] = params["plan_ref"]
    return call_params


async def _fleet_load(mcp: Any, params: dict[str, Any], execute: bool) -> Any:
    """``manage(action="fleet.load")``: mount fleet servers/tools ahead of use."""
    tools = _bounded_names(params, "tools")
    servers = _bounded_names(params, "servers")

    async def _run() -> Any:
        mux = _fleet_mux(mcp)
        mux.require_capability("delegate")
        mounted, available, failed = await mux.resolve_and_mount(
            tools=tools or None, servers=servers or None
        )
        return {"mounted": mounted, "available": available, "failed": failed}

    arguments = {"tools": tools, "servers": servers}
    return await _governed(
        "manage", FLEET_LOAD_ACTION, arguments, params, execute, _run
    )


async def _fleet_unload(mcp: Any, params: dict[str, Any], execute: bool) -> Any:
    """``manage(action="fleet.unload")``: release mounted fleet tools."""
    tools = _bounded_names(params, "tools")

    async def _run() -> Any:
        mux = _fleet_mux(mcp)
        mux.require_capability("delegate")
        released = [name for name in tools if mux.forget_tool(name) is not None]
        return {"released": released}

    return await _governed(
        "manage", FLEET_UNLOAD_ACTION, {"tools": tools}, params, execute, _run
    )


async def _approve(
    intent_cache: IntentCache, mcp: Any, params: dict[str, Any], execute: bool
) -> Any:
    """``manage(action="approve")``: approve one operation for this session."""
    from arbora.mcp_server.backing.arbora_surface import resolve_operation

    op_id = str(params.get("action") or "")
    if resolve_operation(op_id) is None:
        raise ValueError(
            "approve needs params {'action': '<tool>.<op>'} naming a arbora operation."
        )

    async def _run() -> Any:
        _record_approval(intent_cache, op_id)
        return {"approved": op_id, "ttl_seconds": int(IntentCache.TTL_SECONDS)}

    return await _governed(
        "manage", APPROVE_ACTION, {"action": op_id}, params, execute, _run
    )


async def _host_operation(
    mcp: Any, op_id: str, params: dict[str, Any], execute: bool
) -> Any:
    """``act`` on a host-native backing operation (e.g. arbora browser control)."""
    from arbora.mcp_server.backing.arbora_surface import (
        backing_server,
        backing_tools,
        host_operations,
    )
    from arbora.mcp_server.backing.intent_contract import shape_arguments

    tool_name, action = host_operations(mcp)[op_id]
    call_params = {key: value for key, value in params.items() if key != "plan_ref"}
    schema = backing_tools(mcp)[tool_name].parameters or {}
    arguments = shape_arguments(
        frozenset((schema.get("properties") or {}).keys()),
        call_params,
        action,
        accepts_var_keyword=bool(schema.get("additionalProperties")),
    )

    async def _run() -> Any:
        result = await backing_server(mcp).call_tool(tool_name, arguments)
        return {
            "content": [
                item.model_dump(mode="json", by_alias=True, exclude_none=True)
                for item in result.content
            ],
            "structured_content": result.structured_content,
        }

    return await _governed("act", op_id, arguments, params, execute, _run)


def _find_result(candidate: CapabilityCandidate) -> dict[str, Any]:
    """One ``find`` result: the capability plus how to call it."""
    from arbora.mcp_server.backing.arbora_surface import group_for_tool

    actions = _actions_by_tool().get(candidate.tool) or []
    example = operation_id(candidate.tool, actions[0]) if actions else candidate.tool
    verbs = [verb for verb in candidate.verbs if verb in _DISPATCH_VERBS] or ["act"]
    return {
        "source": "arbora",
        "tool": candidate.tool,
        "action": candidate.action,
        "group": group_for_tool(candidate.tool),
        "verbs": list(candidate.verbs),
        "score": round(candidate.score, 4),
        "matched_terms": candidate.matched_terms,
        "how_to_call": (
            f"{verbs[0]}(intent=<same wording>) to let the router choose, "
            f"or {verbs[0]}(action='{example}', params={{...}}); "
            f"find(action='describe', params={{'action': '{example}'}}) gives its arguments."
        ),
    }


def _fleet_how_to_call(hit: dict[str, Any], server: str) -> str:
    """The verb call that reaches one fleet discovery hit (MERISTEM-CONTROL-R031/R032)."""
    prefixed = str(hit.get("prefixed_name") or "")
    if prefixed:
        # MERISTEM-CONTROL-R032: the dotted action id dispatches through the SAME
        # fleet.call path as the indirect params form below.
        dotted = prefixed.replace("__", ".", 1)
        return (
            f"act(action='{dotted}', params={{...}}) calls it directly, or "
            f"act(action='{FLEET_CALL_ACTION}', params={{'tool': '{prefixed}', "
            "'arguments': {...}}) previews the call; resubmit its plan_ref "
            "with execute=true."
        )
    if hit.get("uri"):
        return (
            f"Read {hit['uri']} for the procedure; it names the fleet tools to "
            f"call with act(action='{FLEET_CALL_ACTION}', ...)."
        )
    return (
        f"find(action='catalog', params={{'server': '{server}'}}) lists its tools; "
        f"act(action='{FLEET_CALL_ACTION}', ...) calls one."
    )


def _fleet_find_row(hit: dict[str, Any]) -> dict[str, Any]:
    """One fleet hit shaped like a native capability row (unordered)."""
    server = str(hit.get("server") or "")
    name = str(hit.get("tool") or hit.get("skill") or server)
    return {
        "source": "fleet",
        "kind": str(hit.get("kind") or "tool"),
        "server": server,
        "tool": name,
        "prefixed_name": hit.get("prefixed_name"),
        "fleet_score": hit.get("score"),
        "how_to_call": _fleet_how_to_call(hit, server),
    }


def _native_entries(
    native: list[dict[str, Any]], local: Sequence[CapabilityCandidate]
) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        (f"native:{i}", str(row.get("tool") or ""), _port_text(c), row)
        for i, (row, c) in enumerate(zip(native, local, strict=False))
    ]


def _fleet_entries(
    fleet_hits: list[Any],
) -> list[tuple[str, str, str, dict[str, Any]]]:
    hits = [hit for hit in fleet_hits if isinstance(hit, dict)]
    return [
        (
            f"fleet:{i}",
            f"{row['server']} {row['tool']}",
            str(hit.get("description") or ""),
            row,
        )
        for i, (hit, row) in enumerate(
            zip(hits, (_fleet_find_row(hit) for hit in hits), strict=True)
        )
    ]


def _merge_fleet_results(
    payload: dict[str, Any],
    intent: str,
    fleet_hits: list[Any],
    top_k: int,
    local: Sequence[CapabilityCandidate],
    ranking: RankingPort = FLOOR_ONLY,
) -> None:
    """Order fleet hits alongside native capabilities in ``results`` by the port."""
    entries = _native_entries(list(payload["results"]), local) + _fleet_entries(
        fleet_hits
    )
    rows = [RankCandidate(i, n, d, "tool") for i, n, d, _ in entries]
    ordered = _ordered(ranking, "catalog.intent_action_rank", intent, rows)
    shown = evidence(intent, ((i, n, d) for i, n, d, _ in entries))
    by_id = {i: row for i, _n, _d, row in entries}
    merged = [
        {**by_id[i], **_evidence_fields(by_id[i], shown[i])}
        for i in ordered.order[:top_k]
    ]
    payload["results"] = merged
    payload["count"] = len(merged)


def _evidence_fields(
    row: dict[str, Any], shown: tuple[float, list[str]]
) -> dict[str, Any]:
    """A fleet row carries the floor's evidence; a native row keeps its own."""
    if row.get("source") != "fleet":
        return {}
    return {"score": round(shown[0], 4), "matched_terms": shown[1]}


async def _find_capability(
    caches: HostCaches, mcp: Any, intent: str, top_k: int = 8
) -> dict[str, Any]:
    """Capability discovery across every verb, plus a best-effort fleet-wide search.

    ``mcp`` is the live FastMCP server — the fleet multiplexer, when attached,
    is stashed on it as ``mcp._fleet_mux`` so this can widen the search
    fleet-wide without a second multiplexer instance. Absent (embedded/headless
    builds) it degrades to local-only results — never an error.
    """
    security_failure = _intent_security_failure(intent)
    if security_failure is not None:
        return security_failure
    ranking = _port_of(mcp)
    local = await run_with_port(
        ranking, resolve_intent, caches, None, intent, top_k=top_k
    )
    payload: dict[str, Any] = {
        "query_ref": persistence_reference("intent", intent),
        "count": len(local),
        "results": [_find_result(c) for c in local],
    }
    try:
        mux = getattr(mcp, "_fleet_mux", None)
        if mux is not None:
            mux.require_capability("discover")
            discovery = await mux.discover_tools(intent, top_k=top_k, loaded=set())
            payload["fleet_results"] = discovery.get("results", [])
            payload["fleet_unavailable"] = discovery.get("unavailable", {})
            await run_with_port(
                ranking,
                _merge_fleet_results,
                payload,
                intent,
                payload["fleet_results"],
                top_k,
                local,
            )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "remote discovery health is reported elsewhere", exc_info=True
        )
        payload["fleet_error"] = type(exc).__name__
    return payload


async def _lakehouse_status(hints: dict[str, Any]) -> dict[str, Any]:
    """CA-28's ``manage(action="lakehouse_status")`` surface (design: policy-
    bundle epoch = what CA-26 last applied, index-rebuild trigger = CA-24's
    indexer, CDC-lag = CA-21's consumer) — read-only status plus pointing at
    the single trigger action, NOT a new subsystem (this program's non-goal
    for CA-28).

    Each sub-surface degrades to a typed ``"unavailable"`` entry instead of
    raising when its owning lane's real module has not landed yet — a status
    read must never fail closed just because a dependency lane is mid-flight.
    """
    out: dict[str, Any] = {}
    # Index-rebuild trigger — CA-24 already landed a REAL, working rebuild
    # (`graph_ingest` `action="opensearch_reindex"`, DEC-CA-09): this surfaces
    # it as discoverable status/how-to-call under `manage`, not a second
    # implementation of the rebuild itself.
    out["index_rebuild"] = {
        "status": "available",
        "owner": "CA-24",
        "how_to_call": (
            "graph_ingest(action='opensearch_reindex', "
            "corpus_name=<eg graph/tenant id>)"
        ),
    }
    # CDC-lag — CA-21's Debezium consumer (kafka_adapter.py) has no lag-
    # measurement function yet. Same ``getattr`` rationale as above.
    refusal = not_served(
        "knowledge_graph.streams.kafka_adapter", "MEDULLA-UNIFIED-DATA-PLANE-R008.700"
    )
    out["cdc_lag"] = {"status": "error", "detail": type(refusal).__name__}
    return out


async def _host_dispatch(
    caches: HostCaches,
    mcp: Any,
    verb: str,
    action: str,
    params: dict[str, Any],
    execute: bool,
) -> Any:
    """Serve a verb's operation outside the manifest, or ``None`` if not one."""
    from arbora.mcp_server.backing.arbora_surface import (
        host_operations,
        resolve_operation,
    )

    handlers = {
        ("act", FLEET_CALL_ACTION): _fleet_call,
        ("manage", APPROVE_ACTION): partial(_approve, caches.intent),
        ("manage", FLEET_LOAD_ACTION): _fleet_load,
        ("manage", FLEET_UNLOAD_ACTION): _fleet_unload,
    }
    handler = handlers.get((verb, action))
    if handler is not None:
        return await handler(mcp, params, execute)
    if verb == "manage" and action == LAKEHOUSE_STATUS_ACTION:
        status = await _lakehouse_status(params)
        return {"executed": True, "action": action, "status": status}
    if verb == "act" and action in host_operations(mcp):
        return await _host_operation(mcp, action, params, execute)
    # MERISTEM-CONTROL-R032: act(action='<server>.<tool>') dispatches a fleet tool
    # through the SAME fleet.call path, without the caller needing the
    # indirect act(action='fleet.call', params={'tool': ..., 'arguments': {...}})
    # wrapping. The native manifest (resolve_operation) always wins a real
    # collision; this is reached only when `action` is not a recognized
    # native operation.
    if verb == "act" and action and resolve_operation(action) is None:
        fleet_tool = _fleet_dotted_tool_name(mcp, action)
        if fleet_tool is not None:
            return await _fleet_call(
                mcp, _fleet_dispatch_params(fleet_tool, params), execute
            )
    return None


def _explicit_operation_hints(
    tools: ToolTable, verb: str, action: str, params: dict[str, Any]
) -> dict[str, Any]:
    """Router hints for ``verb(action=<manifest operation id>)``.

    Raises ``ValueError`` for an unknown operation or one ``verb`` does not
    accept.
    """
    from arbora.mcp_server.backing.arbora_surface import resolve_operation

    resolved = resolve_operation(action)
    if resolved is None:
        raise ValueError(
            f"Unknown operation {action!r}; {verb}(action='describe') lists them."
        )
    feature = OPTIONAL_TOOL_FEATURES.get(resolved[0])
    if feature and resolved[0] not in tools:
        raise ValueError(
            f"{action!r} needs the optional '{feature}' feature, which this "
            "deployment does not install."
        )
    verbs = operation_verbs().get(action, ())
    if verb not in verbs:
        raise ValueError(
            f"{action!r} is not a {verb} operation; use one of {list(verbs)}."
        )
    return _operation_hints(tools, resolved[0], resolved[1], params)


async def _dispatch_verb(
    caches: HostCaches,
    mcp: Any,
    verb: str,
    action: str,
    params: dict[str, Any],
    intent: str,
    execute: bool,
) -> Any:
    """The intent contract over the governed router (see module docstring)."""
    if action == DESCRIBE_ACTION:
        return _describe(mcp, verb, params)
    if verb == "find":
        return await _find(caches, mcp, action, params, intent)
    security_failure = _intent_security_failure(intent, params)
    if security_failure is not None:
        return security_failure
    try:
        hosted = (
            await _host_dispatch(caches, mcp, verb, action, params, execute)
            if action
            else None
        )
        if hosted is not None:
            return hosted
        hints = (
            _explicit_operation_hints(caches.tools, verb, action, params)
            if action
            else params
        )
    except ValueError as exc:
        return {
            "error": exc.args[0] if exc.args else "invalid params",
            "executed": False,
        }
    return await dispatch_intent(
        caches, verb, intent, hints=hints, execute=execute, mcp=mcp
    )


def register_intent_tools(mcp: Any, caches: HostCaches) -> list[str]:
    """Register arbora's six intent tools (CONCEPT:MERISTEM-ECO.mcp.intent-surface-condensed-collapse).

    Each takes the ecosystem's condensed contract (``action`` + ``params`` +
    optional ``intent`` + ``execute``; :mod:`arbora.mcp_server.backing.intent_contract`)
    and runs through :func:`dispatch_intent` — the same governed router and the
    same ``_execute_tool`` core the backing operations use. Every one also gets a
    tool-table entry and a REST twin (``runtime.ACTION_TOOL_ROUTES``)
    so MCP and REST dispatch it identically.
    """
    from arbora.mcp_server.backing.intent_contract import make_intent_tool

    registered: list[str] = []
    for verb in INTENT_VERBS:

        def _handler(action, params, intent, execute, _verb=verb):
            return _dispatch_verb(caches, mcp, _verb, action, params, intent, execute)

        async def _run(action, params, intent, execute, _handle=_handler) -> str:
            return json.dumps(
                await _handle(action, params, intent, execute), default=str
            )

        fn = make_intent_tool(
            verb,
            _run,
            actions=FIND_ACTIONS if verb == "find" else None,
            execute_default=verb in _READ_ONLY_VERBS or verb == "find",
        )
        fn.__doc__ = _verb_description(verb)
        mcp.tool(name=verb, tags={"intent"}, description=_verb_description(verb))(fn)
        caches.tools[verb] = fn
        # REST twin (CONCEPT:MERISTEM-ECO.mcp.two-surfaces-mcp-rest) — the generic
        # ACTION_TOOL_ROUTES loop in _mount_rest_routes wires this automatically.
        runtime.ACTION_TOOL_ROUTES[verb] = f"/intent/{verb}"
        registered.append(verb)
    return registered
