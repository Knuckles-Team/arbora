"""Auto-extracted arbora MCP tools: state_tools (register_state_tools).

Split out of runtime._build_server to deepen the MCP surface into focused
modules without changing tool behavior or names.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlencode

from pydantic import Field
from starlette.responses import JSONResponse

from arbora.api.errors import not_served
from arbora.mcp_server import runtime
from arbora.security.error_surface import (
    public_error_json,
)

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
    from arbora.gateway.fleet import fleet_health, fleet_topology

    query: dict[str, str | int] = {"limit": limit, "offset": offset}
    if status:
        query["status"] = status
    req.scope["query_string"] = urlencode(query).encode("ascii")
    if action == "health":
        return await fleet_health(req)
    return await fleet_topology(req)


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
    return None, json.dumps({"error": f"unknown sessions action {action!r}"})


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


def register_state_tools(mcp):
    """Register the state_tools group on the given FastMCP server."""

    @mcp.tool(
        name="graph_sessions",
        description=(
            "Manage durable sessions (action in 'list', 'get', 'delete', 'reply', "
            "'cancel', 'health', 'topology'). Health and topology share the "
            "fail-closed fleet evidence contract with REST."
        ),
        tags=["arbora", "sessions"],
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
        name="graph_feeds",
        description=(
            "Manage the unified RSS/Atom feed registry (CONCEPT:AU-KG.ingest.rss-feed-connector/2.122). "
            "Native RSS feeds, the FreshRSS aggregator, and ScholarX arXiv are "
            "first-class :FeedSource nodes ingested through ONE world-model gate "
            "(research items → prioritized fetch, news → relevance+novelty). action "
            "'sync' (run the feed sweep now — native RSS + ScholarX through the "
            "gate; feeds are fetched concurrently)."
        ),
        tags=["arbora", "feeds"],
    )
    async def graph_feeds(
        action: str = Field(default="sync", description="sync"),
        url: str = Field(default="", description="Feed URL to sync (empty syncs all)."),
        mode: str = Field(default="delta", description="delta|full (sync)."),
    ) -> str:
        """Sync unified RSS feed sources."""
        import json as _json

        try:
            engine = runtime._get_engine()
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
