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

from agent_utilities.core.event_loop import run_blocking_ordered
from agent_utilities.security.error_surface import (
    public_error_json,
    public_error_payload,
)
from pydantic import Field
from starlette.responses import JSONResponse

from graph_os.api.errors import not_served
from graph_os.mcp_server import runtime

logger = logging.getLogger(__name__)


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
    from agent_utilities.core.sessions import (
        cancel_session_run,
        delete_session,
        get_all_sessions,
        get_session_details,
        submit_session_reply,
    )

    if action == "list":
        return await get_all_sessions(req), None
    if action == "get":
        if not session_id:
            return None, json.dumps({"error": "session_id is required"})
        return await get_session_details(req), None
    if action == "delete":
        if not session_id:
            return None, json.dumps({"error": "session_id is required"})
        return await delete_session(req), None
    if action == "reply":
        if not session_id:
            return None, json.dumps({"error": "session_id is required"})
        return await submit_session_reply(req), None
    if action == "cancel":
        if not session_id:
            return None, json.dumps({"error": "session_id is required"})
        return await cancel_session_run(req), None
    return None, json.dumps({"error": f"Unknown sessions action: {action}"})


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
    from agent_utilities.core.sessions import (
        cancel_goal,
        create_goal,
        get_goal_iterations,
        list_goals,
    )

    req = runtime._build_dummy_request(
        path_params={"goal_id": goal_id} if goal_id else {},
        json_body={"objective": goal, "max_iterations": max_iterations}
        if action == "create"
        else None,
    )
    if action == "list":
        return await list_goals(req), None
    if action == "create":
        if not goal:
            return None, json.dumps({"error": "goal is required"})
        return await create_goal(req), None
    if action == "iterations":
        if not goal_id:
            return None, json.dumps({"error": "goal_id is required"})
        req_iter = runtime._build_dummy_request(path_params={"goal_id": goal_id})
        return await get_goal_iterations(req_iter), None
    if action == "cancel":
        if not goal_id:
            return None, json.dumps({"error": "goal_id is required"})
        req_cancel = runtime._build_dummy_request(path_params={"goal_id": goal_id})
        return await cancel_goal(req_cancel), None
    return None, json.dumps({"error": f"Unknown goals action: {action}"})


def _sandbox_reap_action() -> str:
    """``"reap"`` action of ``register_state_tools``'s ``graph_sandbox``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.runtime.warm_registry import WarmParentRegistry

    reaped = WarmParentRegistry.get().reap()
    workspaces: list[str] = []
    try:
        from agent_utilities.runtime.docker_workspace import DockerWorkspace

        workspaces = DockerWorkspace.reap_idle()
    except Exception:  # noqa: BLE001 - dev-workspace reap is best-effort
        pass
    return _json.dumps(
        {
            "action": "reap",
            "reaped_parent_count": len(reaped),
            "reaped_workspace_count": len(workspaces),
            "pool": WarmParentRegistry.get().stats(),
        }
    )


async def _sandbox_warm_action(rung: str) -> str:
    """``"warm"`` action of ``register_state_tools``'s ``graph_sandbox``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    if not rung:
        return _json.dumps({"error": "warm requires an approved rung"})
    raise not_served(
        "rlm.sandboxes.base", "AU-harness (deleted from AU)", owner="agent-utilities"
    )


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
    import json as _json

    from agent_utilities.automation.feed_sources import list_feed_sources

    return _json.dumps(
        {"action": "list", "feeds": list_feed_sources(engine)}, default=str
    )


def _feeds_add_action(engine: Any, targets: list[str]) -> str:
    """``"add"`` action of ``register_state_tools``'s ``graph_feeds``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.automation.feed_sources import upsert_feed_source

    if not targets:
        return _json.dumps({"error": "add needs a feed url (url=... or urls=[...])"})
    results: list[dict] = []
    for u in targets:
        try:
            nid = upsert_feed_source(
                engine,
                key=u,
                source_system="rss",
                feed_url=u,
                kind="RssFeed",
            )
            results.append({"url": u, "id": nid})
        except Exception as e:  # noqa: BLE001 — one bad feed never aborts the batch
            results.append(public_error_payload(e))
    added = [r for r in results if "id" in r]
    return _json.dumps(
        {
            "action": "add",
            "added": len(added),
            "total": len(targets),
            "results": results,
        }
    )


def _feeds_remove_action(engine: Any, targets: list[str]) -> str:
    """``"remove"`` action of ``register_state_tools``'s ``graph_feeds``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.automation.feed_sources import remove_feed_source

    if not targets:
        return _json.dumps({"error": "remove needs a feed url (url=... or urls=[...])"})
    results: list[dict] = []
    for u in targets:
        try:
            ok = remove_feed_source(engine, key=u, source_system="rss")
            results.append({"url": u, "ok": bool(ok)})
        except Exception as e:  # noqa: BLE001
            results.append(public_error_payload(e))
    return _json.dumps(
        {
            "action": "remove",
            "removed": sum(1 for r in results if r.get("ok")),
            "total": len(targets),
            "results": results,
        }
    )


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
    raise not_served("knowledge_graph.core.source_sync", "EG-INGEST-WORKER-R605")


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
    import json as _json

    from agent_utilities.orchestration.agent_digital_twin import (
        VersionPins,
        capture_twin,
        capture_twin_from_kg,
        persist_twin,
    )

    engine = runtime._get_engine()
    pins = VersionPins.from_dict(_parse_json_or(p.versions, {}))
    decisions = _parse_json_or(p.policy_decisions, [])
    evidence_items = _parse_json_or(p.evidence, [])
    explicit_calls = _parse_json_or(p.tool_calls, [])
    explicit_exchanges = _parse_json_or(p.model_exchanges, [])

    if explicit_calls or explicit_exchanges:
        # EXPLICIT-DATA path (capture_twin) — the canonical path a live
        # run (or a test standing in for one) uses: build the twin
        # straight from data the caller already collected, never
        # re-derived from the KG.
        twin_obj = capture_twin(
            agent_name=p.agent_name,
            task=p.task,
            versions=pins,
            run_id=p.run_id or None,
            budget=_json.loads(p.budget) if p.budget and p.budget != "{}" else {},
            work_item_ids=_parse_json_or(p.work_item_ids, []),
            tool_calls=explicit_calls,
            model_exchanges=explicit_exchanges,
            policy_decisions=decisions,
            evidence=evidence_items,
            outcome=p.outcome or "succeeded",
            engine=engine,
        )
    else:
        # KG-HYDRATION path (capture_twin_from_kg) — best-effort read of
        # an already-running KG's existing :ToolCall/:WorkItem rows.
        if not p.run_id:
            return _json.dumps(
                {
                    "error": "twin_capture requires run_id (or explicit "
                    "tool_calls/model_exchanges for the explicit-data path)"
                }
            )
        twin_obj = capture_twin_from_kg(
            engine,
            p.run_id,
            agent_name=p.agent_name,
            task=p.task,
            versions=pins,
            outcome=p.outcome,
            policy_decisions=decisions,
            evidence=evidence_items,
        )
    node_id = persist_twin(engine, twin_obj) if p.persist else None
    return _json.dumps(
        {
            "action": "twin_capture",
            "twin_id": twin_obj.twin_id,
            "node_id": node_id,
            "twin": twin_obj.to_dict(),
        },
        default=str,
    )


def _runvcs_twin_replay_action(twin_obj: Any) -> str:
    """``"twin_replay"`` action of ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.orchestration.agent_digital_twin import replay_twin

    report = replay_twin(twin_obj)
    return _json.dumps(
        {
            "action": "twin_replay",
            "run_id": report.run_id,
            "twin_id": report.twin_id,
            "deterministic": report.deterministic,
            "steps": report.regression.steps,
            "model_calls": report.regression.model_calls,
        }
    )


def _runvcs_twin_counterfactual_action(twin_obj: Any, p: _RunVcsParams) -> str:
    """``"twin_counterfactual"`` action of ``register_state_tools``'s
    ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.orchestration.agent_digital_twin import (
        VersionPins,
        counterfactual_replay,
    )

    versions_override = (
        VersionPins.from_dict(_json.loads(p.versions))
        if p.versions and p.versions != "{}"
        else None
    )
    overrides = _parse_json_or(p.policy_overrides, None)
    responses = _parse_json_or(p.model_responses, None)
    report = counterfactual_replay(
        twin_obj,
        versions=versions_override,
        policy_overrides=overrides,
        model_responses=responses,
    )
    return _json.dumps(
        {
            "action": "twin_counterfactual",
            "run_id": report.run_id,
            "twin_id": report.twin_id,
            "diverged": report.diverged,
            "deterministic": report.deterministic,
            "version_delta": report.version_delta,
            "decision_delta": report.decision_delta,
        },
        default=str,
    )


def _runvcs_twin_incident_action(twin_obj: Any) -> str:
    """``"twin_incident"`` action of ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.orchestration.agent_digital_twin import twin_incident_steps

    steps = twin_incident_steps(twin_obj)
    return _json.dumps(
        {"action": "twin_incident", "run_id": twin_obj.run_id, "steps": steps},
        default=str,
    )


def _runvcs_twin_family_action(action: str, p: _RunVcsParams) -> str:
    """``twin_replay``/``twin_counterfactual``/``twin_incident`` dispatch for
    ``register_state_tools``'s ``graph_runvcs``.

    Extracted verbatim (pure extract-method, no behaviour change).
    """
    import json as _json

    from agent_utilities.orchestration.agent_digital_twin import AgentDigitalTwin

    if not p.twin:
        return _json.dumps(
            {"error": f"{action} requires `twin` (JSON from action='twin_capture')"}
        )
    twin_obj = AgentDigitalTwin.from_dict(_json.loads(p.twin))

    if action == "twin_replay":
        return _runvcs_twin_replay_action(twin_obj)
    if action == "twin_counterfactual":
        return _runvcs_twin_counterfactual_action(twin_obj, p)
    # action == "twin_incident"
    return _runvcs_twin_incident_action(twin_obj)


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
        import json as _json

        from agent_utilities.core import schedule_engine as _se

        try:
            engine = runtime._get_engine()
            if action == "list":
                return _json.dumps(
                    {"action": "list", "schedules": _se.calendar(engine)},
                    default=str,
                )
            if not name:
                return _json.dumps({"error": f"{action} needs a schedule name"})
            if action == "enable":
                return _json.dumps(_se.set_enabled(engine, name, True))
            if action == "disable":
                return _json.dumps(_se.set_enabled(engine, name, False))
            if action == "prioritize":
                return _json.dumps(_se.set_priority(engine, name, priority))
            if action == "set_interval":
                return _json.dumps(_se.set_interval(engine, name, interval_s))
            if action == "run_now":
                return _json.dumps(_se.run_now(engine, name))
            return _json.dumps({"error": f"unknown action {action!r}"})
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

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
        import json as _json

        from agent_utilities.runtime.run_vcs.replay import replay_run
        from agent_utilities.runtime.run_vcs.run_session import RunSessionRegistry

        registry = RunSessionRegistry.get()
        p = _RunVcsParams(
            run_id=run_id,
            commit_id=commit_id,
            label=label,
            twin=twin,
            agent_name=agent_name,
            task=task,
            versions=versions,
            outcome=outcome,
            persist=persist,
            tool_calls=tool_calls,
            model_exchanges=model_exchanges,
            policy_decisions=policy_decisions,
            evidence=evidence,
            budget=budget,
            work_item_ids=work_item_ids,
            policy_overrides=policy_overrides,
            model_responses=model_responses,
        )
        try:
            if action == "list":
                return _json.dumps({"action": "list", "runs": registry.list_ids()})

            # ── Agent Digital Twin actions (CONCEPT:AU-ORCH.twin.agent-digital-twin, X-8) ──
            # Twins project a PAST run independently of any live RunSessionRegistry entry,
            # so these branch out before the live-session guard below.
            if action == "twin_capture":
                return _runvcs_twin_capture_action(p)
            if action in ("twin_replay", "twin_counterfactual", "twin_incident"):
                return _runvcs_twin_family_action(action, p)

            session = registry.acquire(run_id) if run_id else None
            if action != "list" and session is None:
                return _json.dumps(
                    {
                        "error": f"no live run session {run_id!r}",
                        "runs": registry.list_ids(),
                    }
                )
            assert session is not None  # for type-narrowing (guarded above)

            return await _runvcs_live_session_action(
                action, session, registry, p, replay_run
            )
        except Exception as e:  # noqa: BLE001
            return public_error_json(e)

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
