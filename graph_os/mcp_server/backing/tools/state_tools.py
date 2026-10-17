"""Auto-extracted graph-os MCP tools: state_tools (register_state_tools).

Split out of runtime._build_server to deepen the MCP surface into focused
modules without changing tool behavior or names.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from agent_utilities.security.error_surface import (
    public_error_json,
)
from pydantic import Field
from starlette.responses import JSONResponse

from graph_os.core.event_loop import run_blocking_ordered
from graph_os.core.handoff import raise_moved
from graph_os.mcp_server import runtime

logger = logging.getLogger(__name__)


def propose_lakehouse_maintenance_gap(
    engine: Any,
    *,
    source: str,
    statement: str,
    signature: str = "",
    domain: str = "lakehouse-maintenance",
    severity: float = 0.5,
    concept_ids: list[str] | None = None,
) -> dict[str, Any] | None:
    """CA-28's Loop-engine hook: let a lakehouse-maintenance detector propose
    a Transform run or index rebuild the SAME way every other discovery track
    does (CONCEPT:AU-AHE.harness.canonical-gap-lifecycle).

    ``core.schedule_engine``'s three ``lakehouse-maintenance`` dispatch
    targets (``debezium_lag_check``/``opensearch_reindex_staleness_check``/
    ``lineage_sweep`` — CA-21/24/15/25's real detection logic, still to land)
    call this on a genuine finding instead of inventing a second execution
    path: it files one canonical ``:Gap`` via
    :func:`agent_utilities.knowledge_graph.research.gaps.submit_gap` and lets
    it flow through the EXISTING gaps -> SpecProposal -> ``review``
    (approve|edit|reject) -> develop-Loop lifecycle ``graph_loops`` already
    exposes (see :func:`register_state_tools`'s ``graph_loops`` tool, actions
    ``gaps``/``submit_gap``/``review``).

    Deliberately PROPOSE-ONLY — this function has no develop/apply path of
    its own and never mutates lakehouse state, matching ``graph_loops``
    ``run``'s existing ``mine_discovery`` default-ON-but-propose-only
    contract (CONCEPT:AU-KG.evolution.mining-flywheel): a mined/detected
    issue becomes a reviewable proposal, never an automatic change.

    ``signature`` defaults to a stable hash of ``source``+``statement`` so a
    repeated identical finding is idempotent (``submit_gap``'s own
    ``gap:<source>:<signature>`` id dedupes re-detection rather than filing a
    duplicate gap on every tick).
    """
    import hashlib

    from agent_utilities.knowledge_graph.research.gaps import submit_gap

    statement = (statement or "").strip()
    if not statement:
        return None
    sig = signature or hashlib.sha256(f"{source}:{statement}".encode()).hexdigest()[:16]
    return submit_gap(
        engine,
        source=source,
        signature=sig,
        statement=statement,
        domain=domain,
        severity=severity,
        concept_ids=concept_ids or [],
    )


def _format_tool_response(resp: Any) -> str:
    """Format an MCP tool's raw handler response as the final string result:
    decode a ``JSONResponse`` body, else ``str(resp)``. Shared tail used by
    ``graph_sessions``/``graph_goals`` in :func:`register_state_tools`.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    if isinstance(resp, JSONResponse):
        body_bytes = bytes(resp.body)
        return json.dumps(json.loads(body_bytes.decode("utf-8")))
    return str(resp)


async def _resolve_fleet_response(
    action: str, req: Any, limit: int, offset: int, status: str
) -> Any:
    """``health``/``topology`` branch of ``_resolve_sessions_response``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    from graph_os.gateway.fleet import fleet_health, fleet_topology

    query: dict[str, str | int] = {"limit": limit, "offset": offset}
    if status:
        query["status"] = status
    req.scope["query_string"] = urlencode(query).encode("ascii")
    if action == "health":
        return await fleet_health(req)
    return await fleet_topology(req)


async def _resolve_session_crud_response(
    action: str, req: Any, session_id: str
) -> tuple[Any, str | None]:
    """``list``/``get``/``delete``/``reply``/``cancel`` branch of
    ``_resolve_sessions_response``.

    Extracted verbatim (pure extract-method, no behaviour change). Returns
    ``(resp, error_json)``; when ``error_json`` is not ``None`` the caller
    should return it directly instead of formatting ``resp``.
    """
    raise_moved("_resolve_session_crud_response", "core.sessions", "the agent harness")


async def _resolve_sessions_response(
    action: str,
    session_id: str,
    user_reply: str,
    limit: int,
    offset: int,
    status: str,
) -> tuple[Any, str | None]:
    """Resolve the raw response object for one ``graph_sessions`` action, or
    an already-JSON-encoded validation-error string, for
    ``register_state_tools``'s ``graph_sessions`` tool.

    Extracted verbatim (pure extract-method, no behaviour change). Returns
    ``(resp, error_json)``; when ``error_json`` is not ``None`` the caller
    should return it directly instead of formatting ``resp``.
    """
    req = runtime._build_dummy_request(
        path_params={"session_id": session_id} if session_id else {},
        json_body={"content": user_reply} if user_reply else None,
    )
    if action in {"health", "topology"}:
        resp = await _resolve_fleet_response(action, req, limit, offset, status)
        return resp, None
    return await _resolve_session_crud_response(action, req, session_id)


async def _resolve_goals_response(
    action: str, goal_id: str, goal: str, max_iterations: int
) -> tuple[Any, str | None]:
    """The raw response for one ``graph_goals`` action as ``(resp, error_json)``
    (see :func:`_resolve_session_crud_response`)."""
    raise_moved("_resolve_goals_response", "core.sessions", "the agent harness")


def _sandbox_status_action() -> str:
    """``"status"`` action of ``register_state_tools``'s ``graph_sandbox``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.deployment.doctor import _check_warm_fork
    from agent_utilities.rlm.sandboxes.reward import SandboxRewardTracker

    res = _check_warm_fork()
    data = res.get("data") or {}
    return _json.dumps(
        {
            "action": "status",
            "status": res.get("status"),
            "detail": res.get("detail"),
            "rungs": data.get("rungs", {}),
            "warm_rungs": data.get("warm_rungs", []),
            "pool": data.get("pool", {}),
            "rewards": SandboxRewardTracker.get().snapshot(),
        },
        default=str,
    )


def _sandbox_reap_action() -> str:
    """``"reap"`` action of ``register_state_tools``'s ``graph_sandbox``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved("_sandbox_reap_action", "runtime.warm_registry", "the agent harness")


async def _sandbox_warm_action(rung: str) -> str:
    """``"warm"`` action of ``register_state_tools``'s ``graph_sandbox``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved("_sandbox_warm_action", "runtime.warm_registry", "the agent harness")


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    """Dedupe ``items``, keeping first-seen order and dropping falsy
    entries, for ``_resolve_feed_urls``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    seen: set[str] = set()
    deduped: list[str] = []
    for u in items:
        if u and u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def _resolve_feed_urls(url: str, urls: str) -> list[str]:
    """Resolve ``url`` + ``urls`` into a deduped, ordered list (JSON array
    or delimited), for ``register_state_tools``'s ``graph_feeds``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json
    import re as _re

    out: list[str] = []
    raw = (urls or "").strip()
    if raw:
        parsed: object = None
        try:
            parsed = _json.loads(raw)
        except Exception:  # noqa: BLE001 — not JSON → fall back to delimiters
            parsed = None
        if isinstance(parsed, list):
            out.extend(str(x).strip() for x in parsed)
        else:
            out.extend(p.strip() for p in _re.split(r"[,\n]", raw))
    if url:
        out.append(url.strip())
    return _dedupe_preserve_order(out)


def _feeds_list_action(engine: Any) -> str:
    """``"list"`` action of ``register_state_tools``'s ``graph_feeds``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved("_feeds_list_action", "automation.feed_sources", "epistemic-graph")


def _feeds_add_action(engine: Any, targets: list[str]) -> str:
    """``"add"`` action of ``register_state_tools``'s ``graph_feeds``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved("_feeds_add_action", "automation.feed_sources", "epistemic-graph")


def _feeds_remove_action(engine: Any, targets: list[str]) -> str:
    """``"remove"`` action of ``register_state_tools``'s ``graph_feeds``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved("_feeds_remove_action", "automation.feed_sources", "epistemic-graph")


def _feeds_sync_action(engine: Any, url: str, mode: str) -> str:
    """``"sync"`` action of ``register_state_tools``'s ``graph_feeds``: enqueue
    a ``feed_sweep`` task off the request path (CONCEPT:AU-KG.ingest.rss-feed-connector),
    or fall back to an inline ``sync_source`` call when the engine has no
    task queue.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    submit = getattr(engine, "submit_task", None)
    feed_source = (url or "rss").strip().lower()
    if callable(submit):
        job_id = submit(
            target_path=f"feed_sweep:{feed_source}",
            is_codebase=False,
            provenance={"feed_sweep": feed_source},
            task_type="feed_sweep",
            priority=2,
            skip_dedupe=True,
            extra_meta={"feed_source": feed_source, "feed_mode": mode},
        )
        return _json.dumps(
            {
                "action": "sync",
                "enqueued": True,
                "job_id": job_id,
                "source": feed_source,
                "mode": mode,
                "note": "sweep runs in the background (connectors lane); "
                "watch the worldview/research lanes drain.",
            }
        )
    # Fallback: no queue (embedded engine) → run inline.
    from agent_utilities.knowledge_graph.core.source_sync import sync_source

    return _json.dumps(sync_source(engine, feed_source, mode=mode), default=str)


def _parse_json_or(raw: str, default: Any) -> Any:
    """Common ``_json.loads(x) if x else default`` pattern used throughout
    ``graph_runvcs``'s twin actions.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    return _json.loads(raw) if raw else default


@dataclass
class _RunVcsParams:
    """Bundled ``graph_runvcs`` field arguments, so its per-action helpers
    stay under the 7-param cap instead of threading each field through
    individually."""

    run_id: str = ""
    commit_id: str = ""
    label: str = ""
    twin: str = ""
    agent_name: str = ""
    task: str = ""
    versions: str = "{}"
    outcome: str = ""
    persist: bool = True
    tool_calls: str = ""
    model_exchanges: str = ""
    policy_decisions: str = ""
    evidence: str = ""
    budget: str = "{}"
    work_item_ids: str = ""
    policy_overrides: str = ""
    model_responses: str = ""


def _runvcs_twin_capture_action(p: _RunVcsParams) -> str:
    """``"twin_capture"`` action of ``register_state_tools``'s
    ``graph_runvcs`` (CONCEPT:AU-ORCH.twin.agent-digital-twin, X-8).

    Extracted verbatim (pure extract-method, no behaviour change) — same
    eager parse order as the original inline code, so a malformed
    ``policy_decisions``/``evidence`` JSON string still raises before the
    ``run_id``-required check, exactly as before.
    """
    raise_moved(
        "_runvcs_twin_capture_action",
        "orchestration.agent_digital_twin",
        "epistemic-graph",
    )


def _runvcs_twin_replay_action(twin_obj: Any) -> str:
    """``"twin_replay"`` action of ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved(
        "_runvcs_twin_replay_action",
        "orchestration.agent_digital_twin",
        "epistemic-graph",
    )


def _runvcs_twin_counterfactual_action(twin_obj: Any, p: _RunVcsParams) -> str:
    """``"twin_counterfactual"`` action of ``register_state_tools``'s
    ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved(
        "_runvcs_twin_counterfactual_action",
        "orchestration.agent_digital_twin",
        "epistemic-graph",
    )


def _runvcs_twin_incident_action(twin_obj: Any) -> str:
    """``"twin_incident"`` action of ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved(
        "_runvcs_twin_incident_action",
        "orchestration.agent_digital_twin",
        "epistemic-graph",
    )


def _runvcs_twin_family_action(action: str, p: _RunVcsParams) -> str:
    """``twin_replay``/``twin_counterfactual``/``twin_incident`` dispatch for
    ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    raise_moved(
        "_runvcs_twin_family_action",
        "orchestration.agent_digital_twin",
        "epistemic-graph",
    )


async def _runvcs_live_session_action(
    action: str,
    session: Any,
    registry: Any,
    p: _RunVcsParams,
    replay_run: Any,
) -> str:
    """``status``/``commit``/``revert``/``fork``/``discard``/``replay``/unknown
    dispatch for an already-resolved live run session, from
    ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    if action == "status":
        return _json.dumps({"action": "status", **session.status()}, default=str)
    if action == "commit":
        commit = await session.commit(p.label)
        return _json.dumps(
            {"action": "commit", "commit_id": commit.commit_id, "label": p.label}
        )
    if action == "revert":
        res = await session.revert(p.commit_id)
        return _json.dumps({"action": "revert", **res}, default=str)
    if action == "fork":
        child = await session.fork(p.commit_id)
        registry.register(child)
        return _json.dumps(
            {
                "action": "fork",
                "parent_run": session.run_id,
                "child_run": child.run_id,
                "from_commit": p.commit_id,
            }
        )
    if action == "discard":
        return _json.dumps({"action": "discard", **session.discard()}, default=str)
    if action == "replay":
        result = replay_run(session.log)
        return _json.dumps(
            {
                "action": "replay",
                "run_id": result.run_id,
                "steps": result.steps,
                "model_calls": result.model_calls,
                "deterministic": result.deterministic,
            }
        )
    return _json.dumps({"error": f"unknown action {action!r}"})


@dataclass
class _LoopsParams:
    """Bundled ``graph_loops`` field arguments, so its per-action helpers
    stay under the 7-param cap instead of threading each field through
    individually."""

    objective: str = ""
    kind: str = "research"
    loop_id: str = ""
    validation_cmd: str = ""
    end_state: str = ""
    skill_ref: str = ""
    max_topics: int = 5
    limit: int = 10
    priority_bucket: int = 2
    spec_id: str = ""
    decision: str = ""
    status: str = ""
    mine_discovery: bool | None = None
    data_json: str = "{}"


@dataclass
class _LoopsCore:
    """The names ``graph_loops`` originally imported EAGERLY (before its
    ``try`` block) — bundled so an ``ImportError`` there still surfaces
    before any action handler runs, exactly as the original inline code
    did, and so the six handlers that need them stay under the param cap."""

    coerce_prio_bucket: Any
    loop_controller_cls: Any
    active_loops: Any
    mark_loop_status: Any
    prioritize_loop: Any
    submit_loop: Any


async def _loops_submit_action(engine: Any, p: _LoopsParams, core: _LoopsCore) -> str:
    """``"submit"`` action of ``register_state_tools``'s ``graph_loops``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    if not p.objective and not p.skill_ref:
        return _json.dumps({"error": "submit needs objective or skill_ref"})
    loop = await run_blocking_ordered(
        core.submit_loop,
        engine,
        p.objective,
        kind=p.kind,  # type: ignore[arg-type]
        validation_cmd=p.validation_cmd,
        end_state=p.end_state,
        skill_ref=p.skill_ref,
        loop_id=p.loop_id,
        prio_bucket=core.coerce_prio_bucket(p.priority_bucket),
    )
    return _json.dumps({"action": "submit", "loop": loop}, default=str)


async def _loops_list_action(engine: Any, p: _LoopsParams, core: _LoopsCore) -> str:
    """``"list"`` action of ``register_state_tools``'s ``graph_loops``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    loops = await run_blocking_ordered(core.active_loops, engine, p.limit)
    return _json.dumps({"action": "list", "loops": loops}, default=str)


async def _loops_run_action(engine: Any, p: _LoopsParams, core: _LoopsCore) -> str:
    """``"run"`` action of ``register_state_tools``'s ``graph_loops``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    rep = await run_blocking_ordered(
        core.loop_controller_cls(engine).run_one_cycle,
        max_topics=p.max_topics,
        mine_discovery=p.mine_discovery,
    )
    return _json.dumps(rep, indent=2, default=str)


async def _loops_drive_action(engine: Any, p: _LoopsParams, core: _LoopsCore) -> str:
    """``"drive"`` action of ``register_state_tools``'s ``graph_loops``: drive
    ONE Loop to completion durably (resume/checkpoint/corrigible,
    CONCEPT:AU-OS.state.unified-durable-state-externalization) — works for
    any kind (research/develop/skill).

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    if not p.loop_id:
        return _json.dumps({"error": "drive needs a loop_id"})
    found_loops = await run_blocking_ordered(
        core.active_loops, engine, max(p.limit, 50)
    )
    target = next(
        (loop_row for loop_row in found_loops if loop_row.get("id") == p.loop_id),
        None,
    )
    if target is None:
        return _json.dumps({"error": f"no active loop {p.loop_id!r}"})
    res = await core.loop_controller_cls(engine).run_loop(target, sleep_s=0)
    return _json.dumps({"action": "drive", "result": res}, default=str)


async def _loops_cancel_action(engine: Any, p: _LoopsParams, core: _LoopsCore) -> str:
    """``"cancel"`` action of ``register_state_tools``'s ``graph_loops``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    if not p.loop_id:
        return _json.dumps({"error": "cancel needs a loop_id"})
    ok = await run_blocking_ordered(
        core.mark_loop_status, engine, p.loop_id, "cancelled", source="user"
    )
    return _json.dumps({"action": "cancel", "id": p.loop_id, "ok": ok})


async def _loops_prioritize_action(
    engine: Any, p: _LoopsParams, core: _LoopsCore
) -> str:
    """``"prioritize"`` action of ``register_state_tools``'s ``graph_loops``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    if not p.loop_id:
        return _json.dumps({"error": "prioritize needs a loop_id"})
    bucket = core.coerce_prio_bucket(p.priority_bucket)
    ok = await run_blocking_ordered(core.prioritize_loop, engine, p.loop_id, bucket)
    return _json.dumps(
        {
            "action": "prioritize",
            "id": p.loop_id,
            "prio_bucket": bucket,
            "ok": ok,
        }
    )


async def _loops_state_action(engine: Any, p: _LoopsParams) -> str:
    """``"state"`` action of ``register_state_tools``'s ``graph_loops``: LIVE
    EvolutionState (CONCEPT:AU-KG.research.evolutionstate-live-surface-per/2.291)
    — current stage + why, saturation gauge, open_gaps trend, velocity,
    distilled-spec backlog.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.knowledge_graph.research.evolution_state import (
        read_evolution_state,
    )

    evolution = await run_blocking_ordered(read_evolution_state, engine)
    return _json.dumps(
        {"action": "state", "evolution": evolution}, indent=2, default=str
    )


async def _loops_specs_action(engine: Any, p: _LoopsParams) -> str:
    """``"specs"`` action of ``register_state_tools``'s ``graph_loops``: the
    distilled-spec backlog (CONCEPT:AU-KG.research.close-distill-develop-seam).

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.knowledge_graph.research.spec_proposals import list_specs

    specs = await run_blocking_ordered(
        list_specs, engine, status=(p.status or None), limit=p.limit
    )
    return _json.dumps({"action": "specs", "specs": specs}, default=str)


async def _loops_review_action(engine: Any, p: _LoopsParams) -> str:
    """``"review"`` action of ``register_state_tools``'s ``graph_loops``:
    spec-level review/veto BEFORE develop
    (CONCEPT:AU-OS.config.autonomous-spec-develop-off).

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.knowledge_graph.research.spec_proposals import review_spec

    sid = p.spec_id or p.loop_id
    if not sid or not p.decision:
        return _json.dumps(
            {"error": "review needs spec_id and decision (approve|edit|reject)"}
        )
    review_result = await run_blocking_ordered(
        review_spec, engine, sid, p.decision, reviewer="user"
    )
    return _json.dumps({"action": "review", "result": review_result}, default=str)


async def _loops_gaps_action(engine: Any, p: _LoopsParams) -> str:
    """``"gaps"`` action of ``register_state_tools``'s ``graph_loops``: the
    canonical :Gap backlog (CONCEPT:AU-AHE.harness.canonical-gap-lifecycle,
    Wave 6) every discovery track (failure/research/skill/audit) files
    into — in EG's listing order (ranking is the Decide layer's), excludes
    resolved.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.knowledge_graph.research.gaps import open_gaps

    gaps = await run_blocking_ordered(open_gaps, engine, limit=p.limit)
    return _json.dumps({"action": "gaps", "gaps": gaps}, default=str)


def _decode_submit_gap_json(data_json: str) -> dict[str, Any] | None:
    """Decode + type-check ``submit_gap``'s ``data_json``, for
    ``_parse_submit_gap_fields``. Returns ``None`` if it decodes to
    anything other than a JSON object.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    data = _json.loads(data_json) if data_json else {}
    if not isinstance(data, dict):
        return None
    return data


def _check_submit_gap_required(data: dict[str, Any]) -> tuple[str, str, str] | None:
    """Required-field validation for ``_parse_submit_gap_fields``. Returns
    ``(source, signature, statement)``, or ``None`` if any is missing.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    source = str(data.get("source") or "").strip()
    signature = str(data.get("signature") or "").strip()
    statement = str(data.get("statement") or "").strip()
    if not source or not signature or not statement:
        return None
    return source, signature, statement


def _parse_submit_gap_fields(data_json: str) -> tuple[dict[str, Any], str | None]:
    """Parse + validate ``submit_gap``'s ``data_json`` into the
    ``submit_gap()`` kwargs, for ``_loops_submit_gap_action``.

    Extracted verbatim (pure extract-method, no behaviour change). Returns
    ``(fields, error_json)``; when ``error_json`` is not ``None`` the caller
    should return it directly instead of using ``fields``.
    """
    import json as _json

    data = _decode_submit_gap_json(data_json)
    if data is None:
        return {}, _json.dumps({"error": "data_json must decode to an object"})
    required = _check_submit_gap_required(data)
    if required is None:
        return {}, _json.dumps(
            {"error": "submit_gap needs data_json.source, .signature, and .statement"}
        )
    source, signature, statement = required
    fields = {
        "source": source,
        "signature": signature,
        "statement": statement,
        "domain": str(data.get("domain") or ""),
        "severity": float(data.get("severity", 0.5) or 0.5),
        "concept_ids": [str(c) for c in (data.get("concept_ids") or [])],
    }
    return fields, None


async def _loops_submit_gap_action(engine: Any, p: _LoopsParams) -> str:
    """``"submit_gap"`` action of ``register_state_tools``'s ``graph_loops``:
    the SAME canonical entry point the failure/research/skill/audit
    discovery tracks call internally — exposed here so an operator can file
    one by hand (e.g. a manually-triaged issue).

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.knowledge_graph.research.gaps import submit_gap

    fields, error_json = _parse_submit_gap_fields(p.data_json)
    if error_json is not None:
        return error_json
    gap = await run_blocking_ordered(submit_gap, engine, **fields)
    if gap is None:
        return _json.dumps({"error": "submit_gap failed to persist"})
    return _json.dumps({"action": "submit_gap", "gap": gap}, default=str)


def _gap_provenance(gap: dict[str, Any]) -> dict[str, Any]:
    """The unified provenance chain of one Gap, read off EG's own Gap view:
    the SpecProposal it was SPECIFIED_BY (its latest spec reference) and the
    publication that resolved it (the ``resolved`` evidence entry's reference).
    """
    spec_refs = gap.get("spec_refs") or []
    resolved = [
        e.get("reference")
        for e in gap.get("evidence") or []
        if isinstance(e, dict) and e.get("kind") == "resolved"
    ]
    return {
        "specified_by_spec_id": spec_refs[-1] if spec_refs else None,
        "resolved_by": resolved[-1] if resolved else None,
    }


async def _loops_gap_action(engine: Any, p: _LoopsParams) -> str:
    """``"gap"`` action of ``register_state_tools``'s ``graph_loops``: one
    :Gap plus its unified provenance chain — reuses ``loop_id`` as the
    generic id field, the same convention 'cancel'/'prioritize'/'drive'
    already use.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.knowledge_graph.research.gaps import get_gap

    gap_id = p.loop_id
    if not gap_id:
        return _json.dumps({"error": "gap needs a gap id in loop_id"})
    gap = await run_blocking_ordered(get_gap, engine, gap_id)
    if gap is None:
        return _json.dumps({"action": "gap", "id": gap_id, "error": "gap not found"})
    return _json.dumps(
        {"action": "gap", "gap": gap, "provenance": _gap_provenance(gap)}, default=str
    )


def _build_loops_dispatch(
    engine: Any, p: _LoopsParams, core: _LoopsCore
) -> dict[str, Callable[[], Awaitable[str]]]:
    """Build the ``action -> handler`` dispatch table for
    ``register_state_tools``'s ``graph_loops``. The first six entries close
    over the SAME eagerly-imported ``core`` names the original inline code
    used (so an ``ImportError`` there still surfaces before any handler
    runs, exactly as before extraction); the rest import their own
    dependency lazily, exactly as the original per-branch code did.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    return {
        "submit": lambda: _loops_submit_action(engine, p, core),
        "list": lambda: _loops_list_action(engine, p, core),
        "run": lambda: _loops_run_action(engine, p, core),
        "drive": lambda: _loops_drive_action(engine, p, core),
        "cancel": lambda: _loops_cancel_action(engine, p, core),
        "prioritize": lambda: _loops_prioritize_action(engine, p, core),
        "state": lambda: _loops_state_action(engine, p),
        "specs": lambda: _loops_specs_action(engine, p),
        "review": lambda: _loops_review_action(engine, p),
        "gaps": lambda: _loops_gaps_action(engine, p),
        "submit_gap": lambda: _loops_submit_gap_action(engine, p),
        "gap": lambda: _loops_gap_action(engine, p),
    }


def register_state_tools(mcp):
    """Register the state_tools group on the given FastMCP server."""

    @mcp.tool(
        name="graph_sessions",
        description=(
            "Manage durable sessions (action in 'list', 'get', 'delete', 'reply', "
            "'cancel', 'health', 'topology'). Health and topology share the "
            "fail-closed fleet evidence contract with REST."
        ),
        tags=["graph-os", "sessions"],
    )
    async def graph_sessions(
        action: str = Field(
            description=(
                "Action: 'list', 'get', 'delete', 'reply', 'cancel', 'health', "
                "'topology'"
            )
        ),
        session_id: str = Field(default="", description="Target session ID"),
        user_reply: str = Field(
            default="", description="Reply content for 'reply' action"
        ),
        limit: int = Field(default=200, description="Page size for 'topology'."),
        offset: int = Field(default=0, description="Page offset for 'topology'."),
        status: str = Field(
            default="", description="Optional session status filter for 'topology'."
        ),
    ) -> str:
        """Manage durable sessions and fail-closed fleet supervision."""

        try:
            resp, error_json = await _resolve_sessions_response(
                action, session_id, user_reply, limit, offset, status
            )
            if error_json is not None:
                return error_json
            return _format_tool_response(resp)
        except Exception as e:
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_sessions"] = graph_sessions

    @mcp.tool(
        name="graph_goals",
        description="Orchestrate background/autonomous loops (action in 'create', 'list', 'iterations', 'cancel').",
        tags=["graph-os", "goals"],
    )
    async def graph_goals(
        action: str = Field(
            description="Action: 'create', 'list', 'iterations', 'cancel'"
        ),
        goal_id: str = Field(default="", description="Target goal ID"),
        goal: str = Field(
            default="", description="Goal description/instruction for 'create' action"
        ),
        max_iterations: int = Field(
            default=10, description="Max iterations for the autonomous loop"
        ),
    ) -> str:
        """Orchestrate background/autonomous loops. Action: 'create', 'list', 'iterations', 'cancel'."""

        try:
            resp, error_json = await _resolve_goals_response(
                action, goal_id, goal, max_iterations
            )
            if error_json is not None:
                return error_json
            return _format_tool_response(resp)
        except Exception as e:
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_goals"] = graph_goals

    @mcp.tool(
        name="graph_loops",
        description=(
            "The single entrypoint for long-running objectives (CONCEPT:AU-KG.research.these-properties-carry). A "
            "Loop is one objective of kind research|develop|skill; the LoopController "
            "advances every active Loop through ONE hot path. action in 'submit' "
            "(create a Loop: objective + kind [+ validation_cmd/end_state for develop, "
            "skill_ref for skill]), 'list' (active Loops), 'run' (advance all active "
            "Loops one cycle — research acquires/reasons, develop validates, skill "
            "executes), 'drive' (run ONE Loop by id to completion, durably — "
            "resume/checkpoint/corrigible, any kind), 'cancel' (terminate a Loop by id), "
            "'prioritize' (set claim bucket). TRANSPARENCY + STEERING (KG-2.290/292, "
            "OS-5.73): 'state' (LIVE EvolutionState — current stage + why, saturation "
            "gauge, open_gaps trend, velocity, spec backlog), 'specs' (distilled "
            "SpecProposal backlog; filter by status), 'review' (approve|edit|reject a "
            "distilled spec BEFORE it develops — spec_id + decision). 'run' also takes "
            "``mine_discovery`` (CONCEPT:AU-KG.evolution.mining-flywheel, default ON via "
            "KG_LOOP_MINE_DISCOVERY): the discovery-flywheel mining pass — association-"
            "rule + anomaly + graph_learn link-prediction over the KG's Capability/"
            "Concept nodes, write-back only (propose-only, never auto-merges). "
            "GAP LIFECYCLE (CONCEPT:AU-AHE.harness.canonical-gap-lifecycle, Wave 6 — the "
            "unified Gap->SDD->Implement->Promote->Close spine every discovery track "
            "files into): 'gaps' (the open :Gap backlog, highest priority first), "
            "'submit_gap' (file one canonical :Gap — data_json={source,signature,"
            "statement,domain,severity,concept_ids}), 'gap' (one :Gap by id — reuses "
            "loop_id — plus its provenance chain: the SpecProposal it was "
            "SPECIFIED_BY and the develop-Loop that RESOLVES it, when either exists). "
            "A gap's derived develop-Loop is driven the SAME way as any other Loop "
            "('run'/'drive'); publishing it closes the gap (status -> resolved)."
        ),
        tags=["graph-os", "loops"],
    )
    async def graph_loops(
        action: str = Field(
            default="list",
            description=(
                "submit|list|run|drive|cancel|prioritize|state|specs|review|"
                "gaps|submit_gap|gap"
            ),
        ),
        objective: str = Field(default="", description="Objective text (submit)."),
        kind: str = Field(
            default="research", description="research|develop|skill (submit)."
        ),
        loop_id: str = Field(default="", description="Loop id (submit/cancel)."),
        validation_cmd: str = Field(
            default="",
            description="Shell command whose exit-0 completes a develop Loop.",
        ),
        end_state: str = Field(default="", description="Human end-state (develop)."),
        skill_ref: str = Field(
            default="", description="Skill / skill-workflow name or id (skill Loop)."
        ),
        max_topics: int = Field(default=5, description="Loops to advance per run."),
        limit: int = Field(default=10, description="Max rows (list)."),
        priority_bucket: int = Field(
            default=2,
            ge=0,
            le=3,
            description="Integer WorkItem claim bucket 0-3 (submit/prioritize).",
        ),
        spec_id: str = Field(
            default="", description="SpecProposal id (review action)."
        ),
        decision: str = Field(
            default="",
            description="approve|edit|reject — spec-review decision (review action).",
        ),
        status: str = Field(
            default="",
            description="Filter SpecProposals by status (specs action): "
            "pending_review|approved|developing|published|reverted|rejected.",
        ),
        mine_discovery: bool | None = Field(
            default=None,
            description="'run' only: gate the discovery-flywheel mining stage "
            "(CONCEPT:AU-KG.evolution.mining-flywheel). None (default) falls back to "
            "config.kg_loop_mine_discovery (default True); explicit true/false overrides.",
        ),
        data_json: str = Field(
            default="{}",
            description="'submit_gap' only: JSON object {source, signature, statement, "
            "domain, severity, concept_ids}. source/signature/statement are required; "
            "severity (0..1, default 0.5) maps to the shared priority_bucket.",
        ),
    ) -> str:
        """Submit / list / run / drive / cancel / prioritize Loops + observe & steer
        the self-evolution flywheel (state / specs / review /
        gaps / submit_gap / gap) — the one entrypoint."""
        import json as _json

        from agent_utilities.knowledge_graph.core.engine_tasks import (
            _coerce_prio_bucket,
        )
        from agent_utilities.knowledge_graph.research.loop_controller import (
            LoopController,
        )
        from agent_utilities.knowledge_graph.research.loops import (
            active_loops,
            mark_loop_status,
            prioritize_loop,
            submit_loop,
        )

        p = _LoopsParams(
            objective=objective,
            kind=kind,
            loop_id=loop_id,
            validation_cmd=validation_cmd,
            end_state=end_state,
            skill_ref=skill_ref,
            max_topics=max_topics,
            limit=limit,
            priority_bucket=priority_bucket,
            spec_id=spec_id,
            decision=decision,
            status=status,
            mine_discovery=mine_discovery,
            data_json=data_json,
        )
        core = _LoopsCore(
            coerce_prio_bucket=_coerce_prio_bucket,
            loop_controller_cls=LoopController,
            active_loops=active_loops,
            mark_loop_status=mark_loop_status,
            prioritize_loop=prioritize_loop,
            submit_loop=submit_loop,
        )
        try:
            engine = runtime._get_engine()
            dispatch = _build_loops_dispatch(engine, p, core)
            handler = dispatch.get(action)
            if handler is None:
                return _json.dumps({"error": f"unknown action {action!r}"})
            return await handler()
        except Exception as e:
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_loops"] = graph_loops

    @mcp.tool(
        name="graph_schedules",
        description=(
            "Inspect and control the unified scheduler (CONCEPT:AU-OS.state.unified-scheduling-one-intelligent). Every "
            "recurring job — the deploy/schedules.yml entries, the former "
            "fixed-interval maintenance ticks, the self-evolution loop, and the "
            "ScholarX RSS research feed — is a durable :Schedule node the one "
            "scheduler tick enqueues from. action in 'list' (registry + live "
            "run state), 'enable'/'disable' (toggle by name), 'prioritize' (set "
            "the claim bucket 0-3 of the enqueued job), 'set_interval' (retune "
            "cadence, seconds), 'run_now' (fire on the next tick)."
        ),
        tags=["graph-os", "scheduler"],
    )
    async def graph_schedules(
        action: str = Field(
            default="list",
            description="list|enable|disable|prioritize|set_interval|run_now",
        ),
        name: str = Field(default="", description="Schedule name (all but list)."),
        priority: int = Field(
            default=2,
            ge=0,
            le=3,
            description="Integer claim bucket 0-3 (prioritize).",
        ),
        interval_s: float = Field(
            default=0.0, description="New interval seconds (set_interval)."
        ),
    ) -> str:
        """List / enable / disable / prioritize / retune / run-now schedules."""
        raise_moved("graph_schedules", "core", "epistemic-graph")

    runtime.REGISTERED_TOOLS["graph_schedules"] = graph_schedules

    @mcp.tool(
        name="graph_sandbox",
        description=(
            "Inspect and control the native warm-fork sandbox runtime (CONCEPT:AU-ORCH.sandbox.graph-sandbox-surface). "
            "The RLM code-execution tier boots a runtime warm once and forks children from "
            "copy-on-write state only where the boundary also confines filesystem, "
            "credentials, processes, and network (for example a firecracker microVM). "
            "action in 'status' "
            "(per-rung availability + pooled warm-parent count + per-rung reward EMA), 'reap' "
            "(close idle warm parents now + idle dev-workspaces), 'warm' (pre-pay a rung's "
            "start-up so the next fan-out forks cheaply — name it with rung). Code execution "
            "itself stays inside the governed RLM loop; this surface is lifecycle + visibility."
        ),
        tags=["graph-os", "sandbox", "warm-fork"],
    )
    async def graph_sandbox(
        action: str = Field(default="status", description="status|reap|warm"),
        rung: str = Field(
            default="", description="Approved confined warm-fork rung to warm."
        ),
    ) -> str:
        """Status / reap / warm the warm-fork sandbox rungs (CONCEPT:AU-ORCH.sandbox.graph-sandbox-surface, CONCEPT:AU-OS.host.so-they-are-idle)."""
        import json as _json

        try:
            if action == "status":
                return _sandbox_status_action()
            if action == "reap":
                return _sandbox_reap_action()
            if action == "warm":
                return await _sandbox_warm_action(rung)
            return _json.dumps({"error": "unknown sandbox action"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_sandbox"] = graph_sandbox

    @mcp.tool(
        name="graph_runvcs",
        description=(
            "Agent-native run version-control (CONCEPT:AU-ORCH.runvcs.run-commit): fork, "
            "revert and review a LIVE agent run as content-addressed commits that bind its "
            "conversation + filesystem + process/event frontier into ONE exact world. action "
            "in 'list' (live run sessions), 'status' (a run's event/commit/message counts + "
            "log digest), 'commit' (snapshot messages+fs+events into one RunCommit — pass "
            "label), 'revert' (restore a run's files+process+messages to a commit — pass "
            "commit_id), 'fork' (branch a NEW run from a commit into a fresh workspace, parent "
            "untouched — pass commit_id), 'discard' (drop the uncommitted event delta), "
            "'replay' (deterministically replay the CURRENT live run's event log — a recorded "
            "exchange stands in for the model — and verify reproduction). Retained-output "
            "accept/discard of a finished run is governed by the run.select action-policy gate. "
            "Agent Digital Twin actions (CONCEPT:AU-ORCH.twin.agent-digital-twin, X-8) — a "
            "durable, replayable projection of a PAST run kept independently of any live "
            "session: 'twin_capture' (build a twin two ways — pass `tool_calls` and/or "
            "`model_exchanges` for the EXPLICIT-DATA path [the canonical path a live run, or "
            "a test standing in for one, uses: this run's own already-collected data, mirrored "
            "straight into the twin's event log], or omit both and pass `run_id` for the "
            "KG-HYDRATION path [best-effort read of the KG's already-recorded :ToolCall/"
            ":WorkItem rows for that run id]; either way `policy_decisions`/`evidence` are "
            "attached when passed — they are NEVER auto-discovered from the KG — optionally "
            "persist the result as a :AgentDigitalTwin node, and return the full serialized "
            "twin JSON — pass that JSON back in as `twin` to the actions below), 'twin_replay' "
            "(regression-replay a captured twin — pass `twin`), 'twin_counterfactual' "
            "(re-drive a captured twin under a swapped policy/model version — pass `twin` plus "
            "`policy_overrides` and/or `model_responses`, and optionally `versions` for "
            "reporting), 'twin_incident' (ordered, human-inspectable step-through of a "
            "captured twin's recorded run — pass `twin`)."
        ),
        tags=["graph-os", "runvcs", "fork", "revert", "twin"],
    )
    async def graph_runvcs(
        action: str = Field(
            default="list",
            description=(
                "list|status|commit|revert|fork|discard|replay|twin_capture|twin_replay|"
                "twin_counterfactual|twin_incident"
            ),
        ),
        run_id: str = Field(
            default="",
            description="Target run session id (live-run actions) or run id to hydrate a twin from (twin_capture).",
        ),
        commit_id: str = Field(
            default="", description="Target commit id (revert|fork)."
        ),
        label: str = Field(default="", description="Commit label (commit)."),
        twin: str = Field(
            default="",
            description=(
                "JSON-serialized AgentDigitalTwin (CONCEPT:AU-ORCH.twin.agent-digital-twin) — "
                "the output of action='twin_capture'. Required by twin_replay/"
                "twin_counterfactual/twin_incident."
            ),
        ),
        agent_name: str = Field(
            default="", description="Agent name to stamp on the twin (twin_capture)."
        ),
        task: str = Field(
            default="",
            description="Task description to stamp on the twin (twin_capture).",
        ),
        versions: str = Field(
            default="{}",
            description=(
                "JSON VersionPins fields (model_id, model_provider, prompt_version_id, "
                "tool_versions, skill_versions, policy_version, policy_digest, catalog_epoch). "
                "For twin_capture: the pins this run executed under. For twin_counterfactual: "
                "the swapped pins to diff against the twin's recorded pins (reporting only — "
                "pass policy_overrides/model_responses to actually change the replay outcome)."
            ),
        ),
        outcome: str = Field(
            default="",
            description="Outcome status to stamp on the twin (twin_capture; default 'succeeded').",
        ),
        persist: bool = Field(
            default=True,
            description=(
                "twin_capture: best-effort persist the twin as a durable :AgentDigitalTwin "
                "KG node (no-op without a live engine)."
            ),
        ),
        tool_calls: str = Field(
            default="",
            description=(
                "twin_capture EXPLICIT-DATA path: JSON array of {tool_name, args, result, "
                "error} records — the exact shape "
                "orchestration.tool_provenance.extract_tool_calls returns. Non-empty here "
                "(or in `model_exchanges`) makes twin_capture build the twin from this data "
                "directly (agent_digital_twin.capture_twin) instead of hydrating from the KG."
            ),
        ),
        model_exchanges: str = Field(
            default="",
            description=(
                "twin_capture EXPLICIT-DATA path: JSON array of {request, response} model "
                "exchanges to mirror into the twin's event log."
            ),
        ),
        policy_decisions: str = Field(
            default="",
            description=(
                "twin_capture (either path): JSON array of recorded ActionDecision dicts "
                "(request/decision/tier/reason/rule_origin/approval_id/audit_id) to attach "
                "to the twin. NEVER auto-discovered from the KG (today's "
                "AgentPolicyDecisionNode audit rows carry no edge back to the RunTrace that "
                "produced them) — pass them explicitly when known."
            ),
        ),
        evidence: str = Field(
            default="",
            description=(
                "twin_capture (either path): JSON array of EvidenceBundle-shaped dicts "
                "backing the run's outcome."
            ),
        ),
        budget: str = Field(
            default="{}",
            description=(
                "twin_capture EXPLICIT-DATA path only: JSON budget dict to stamp on the twin."
            ),
        ),
        work_item_ids: str = Field(
            default="",
            description=(
                "twin_capture EXPLICIT-DATA path only: JSON array of WorkItem ids forming "
                "the run's DAG (auto-discovered from the KG instead on the hydration path)."
            ),
        ),
        policy_overrides: str = Field(
            default="",
            description=(
                "JSON policy ruleset {version, defaults, rules} for twin_counterfactual — "
                "recompute every recorded decision under this swapped policy version and "
                "diff against what was originally decided."
            ),
        ),
        model_responses: str = Field(
            default="",
            description=(
                "JSON {request: alternate_response} for twin_counterfactual — substitute an "
                "alternate model/prompt response for a recorded model exchange and surface the "
                "resulting stream divergence."
            ),
        ),
    ) -> str:
        """List / inspect / commit / revert / fork / discard / replay a live run (run-VCS);
        capture / replay / counterfactual-replay / step through an Agent Digital Twin (X-8)."""
        raise_moved("graph_runvcs", "runtime.run_vcs.replay", "the agent harness")

    runtime.REGISTERED_TOOLS["graph_runvcs"] = graph_runvcs

    @mcp.tool(
        name="graph_feeds",
        description=(
            "Manage the unified RSS/Atom feed registry (CONCEPT:AU-KG.ingest.rss-feed-connector/2.122). "
            "Native RSS feeds, the FreshRSS aggregator, and ScholarX arXiv are "
            "first-class :FeedSource nodes ingested through ONE world-model gate "
            "(research items → prioritized fetch, news → relevance+novelty). action "
            "in 'list' (registered feeds), 'add' (register one OR many native "
            "RSS/Atom feeds — pass url=, or urls= a JSON array / comma-separated "
            "list for a bulk add), 'remove' (deregister one or many by url/urls), "
            "'sync' (run the feed sweep now — native RSS + ScholarX through the "
            "gate; feeds are fetched concurrently)."
        ),
        tags=["graph-os", "feeds"],
    )
    async def graph_feeds(
        action: str = Field(default="list", description="list|add|remove|sync"),
        url: str = Field(default="", description="Feed URL (single add/remove)."),
        urls: str = Field(
            default="",
            description=(
                "BULK add/remove: many feed URLs in ONE call — a JSON array "
                '(\'["https://a/feed","https://b/rss"]\') or a comma/newline-'
                "separated string. Combined with `url` and deduped."
            ),
        ),
        mode: str = Field(default="delta", description="delta|full (sync)."),
    ) -> str:
        """List / add / remove / sync unified RSS feed sources (add/remove are bulk-capable)."""
        import json as _json

        try:
            engine = runtime._get_engine()
            if action == "list":
                return _feeds_list_action(engine)
            if action == "add":
                return _feeds_add_action(engine, _resolve_feed_urls(url, urls))
            if action == "remove":
                return _feeds_remove_action(engine, _resolve_feed_urls(url, urls))
            if action == "sync":
                return _feeds_sync_action(engine, url, mode)
            return _json.dumps({"error": f"unknown action {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["graph_feeds"] = graph_feeds

    @mcp.tool(
        name="research_artifact",
        description=(
            "Agent-Native Research Artifacts over the one ontology-driven KG "
            "(CONCEPT:AU-KG.research.best-effort-lightweight-never/2.80). action in 'reason' (run OWL/RDF reasoning over "
            "the whole ecosystem and harvest extrapolated cross-domain relationships "
            "as research topics), 'compile' (paper -> ecosystem-grounded OWL-native "
            "4-layer ARA), 'review'/'seal' (L1/L2/L3 OWL/SHACL-grounded review + "
            "certificate), 'capture' (live research event w/ provenance), 'get', 'list', "
            "'inquire' (native multi-perspective STORM inquiry: expert lenses -> "
            "contradiction/agreement/blind-spot map + self-critique, CONCEPT:AU-KG.research.perspectival-inquiry)."
        ),
        tags=["graph-os", "research", "ontology"],
    )
    async def research_artifact(
        action: str = Field(
            default="reason",
            description="reason|compile|review|seal|capture|get|list|inquire",
        ),
        topic: str = Field(default="", description="Topic to inquire into (inquire)."),
        article_id: str = Field(
            default="", description="Paper/article id (compile/review/get)."
        ),
        query: str = Field(
            default="", description="Topic for 'reason' (reasoning is ecosystem-wide)."
        ),
        level: str = Field(default="L1", description="Seal level: L1|L2|L3 (review)."),
        text: str = Field(default="", description="Event text (capture)."),
        provenance: str = Field(
            default="ai_executed",
            description="capture provenance: user|ai_suggested|ai_executed|user_revised.",
        ),
        actor: str = Field(default="", description="Originating actor id (capture)."),
        event_type: str = Field(default="", description="Force event type (capture)."),
        target_codebase: str = Field(
            default="", description="Codebase to ground claims against (compile)."
        ),
        limit: int = Field(default=50, description="Max rows (list)."),
        materialize: bool = Field(
            default=True, description="Persist inquiry nodes (inquire)."
        ),
    ) -> str:
        """Run an ARA action over the one ontology-driven KG (single SoT)."""

        from agent_utilities.knowledge_graph.research.ara.service import ARAService

        try:
            service = ARAService(runtime._get_engine())
            result = service.run(
                action,
                article_id=article_id,
                topic=topic or query,
                query=query,
                level=level,
                text=text,
                provenance=provenance,
                actor=actor,
                event_type=event_type,
                target_codebase=target_codebase or None,
                limit=limit,
                materialize=materialize,
            )
            return json.dumps(result, default=str)
        except Exception as e:
            return public_error_json(e)

    runtime.REGISTERED_TOOLS["research_artifact"] = research_artifact

    # ══════════════════════════════════════════════════════════════════
    # Ontology System — Palantir Foundry parity (type/link/function layer)
    #   property types  (CONCEPT:AU-KG.ontology.ontology-property-types)
    #   value types     (CONCEPT:AU-KG.ontology.value-type-shacl-load)
    #   interfaces      (CONCEPT:AU-KG.ontology.conformance-check)
    #   links           (CONCEPT:AU-KG.domains.trade-journal-bias-auditor)
    #   functions       (CONCEPT:AU-KG.ontology.default-runtime-bound-import)
    #   derived props   (CONCEPT:AU-KG.ontology.derived-property-registry)
    # All handlers are thin — they reach the live `KnowledgeGraph.ontology`
    # system (bound to the engine's backend) so Functions-on-Objects, derived
    # compute and interface targeting resolve against the real graph.
    # ══════════════════════════════════════════════════════════════════
