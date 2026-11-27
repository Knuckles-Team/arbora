"""The metrics, traces and logs engine-surface tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from pydantic import Field

from arbora.core.environment import read_env
from arbora.mcp_server.backing.tools.engine_surface_common import (
    _client,
    _decode_json_object,
    _degraded,
    _drop_empty,
    _invoke,
    _invoke_callable,
    _json_default,
    _resolve,
    _surface_error,
)
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


_PROMQL_INSTANT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("observability", "promql"),
    ("metrics", "promql"),
    ("observability", "query"),
    ("promql", "query"),
)


_PROMQL_RANGE_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("observability", "promql_range"),
    ("metrics", "query_range"),
    ("observability", "query_range"),
    ("promql", "query_range"),
)


_TRACES_SEARCH_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("observability", "search_traces"),
    ("traces", "search"),
    ("observability", "query_traces"),
)


_TRACES_GET_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("observability", "get_trace"),
    ("traces", "get"),
    ("observability", "trace"),
)


_LOGS_QUERY_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("observability", "query_logs"),
    ("observability", "search_logs"),
    ("logs", "query"),
    ("logs", "search"),
)


def _invoke_promql(
    *,
    action: str,
    graph: str,
    candidates: tuple[tuple[str, str], ...],
    params: dict[str, Any],
) -> str:
    """``graph_promql``'s own dispatch (CONCEPT:MERISTEM-KG.coordination.engine-message-broker):
    the connected engine's native PromQL surface wins when present; only when the
    engine build has NO promql surface does this fall back to a directly
    configured Prometheus HTTP endpoint (:func:`_prometheus_http_query`); only
    when THAT is also unavailable does it degrade cleanly. Mirrors :func:`_invoke`
    otherwise (same error handling for an unreachable engine / bad kwargs / engine
    errors on the native surface).
    """
    surface = "promql"
    try:
        client = _client(graph)
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "engine down is a normal degrade", exc_info=True
        )
        return _surface_error(
            exc,
            surface=surface,
            action=action,
            code="dependency_unavailable",
        )
    fn = _resolve(client, candidates)
    if fn is not None:
        return _invoke_callable(fn, surface=surface, action=action, params=params)
    series = _prometheus_http_query(action, params)
    if series is not None:
        return json.dumps(
            {"surface": surface, "action": action, "result": series},
            default=_json_default,
        )
    return _degraded(surface, action, [".".join((a, m)) for a, m in candidates])


def _prometheus_base_url() -> str | None:
    """The directly configured Prometheus HTTP base URL for ``graph_promql``'s
    fallback (CONCEPT:MERISTEM-KG.coordination.engine-message-broker) — consulted only
    when the connected engine build has no native PromQL surface. Unset by
    default: no engine surface + no URL configured is a clean degrade, never an
    invented hostname.
    """

    url = str(read_env("ENGINE_SURFACE_PROMETHEUS_URL") or "").strip()
    return url or None


def _prometheus_http_query(
    action: str, params: dict[str, Any], *, transport: Any = None
) -> list[Any] | None:
    """Query a directly configured Prometheus HTTP endpoint as ``graph_promql``'s
    fallback when the connected engine build has no native PromQL surface.

    Mirrors ``orchestration.scaling_signals.PrometheusHttpProvider``'s own
    request/parse shape (CONCEPT:MERISTEM-KG.coordination.engine-message-broker):
    any transport/HTTP/parse problem — including "not configured" — degrades to
    a clean ``None`` (no data), never a raised exception.
    """
    base_url = _prometheus_base_url()
    if not base_url:
        return None
    from arbora.core.transport import create_http_client

    path = "/api/v1/query_range" if action == "range" else "/api/v1/query"
    try:
        with create_http_client(timeout=5.0, transport=transport) as client:
            resp = client.get(f"{base_url}{path}", params=params)
            resp.raise_for_status()
            payload = resp.json()
    except (
        httpx.HTTPError,
        OSError,
        ValueError,
    ) as e:  # fallback failures are "no data", never raised
        logger.debug(
            "graph_promql: Prometheus HTTP fallback query failed: %s",
            type(e).__name__,
            exc_info=True,
        )
        return None
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return None
    result = (payload.get("data") or {}).get("result")
    return result if isinstance(result, list) else None


def _traces_get_dispatch(trace_id: str) -> tuple[str | None, dict[str, Any], Any]:
    """``graph_traces action='get'`` — the KG-native sink first, else the engine probe.

    Returns ``(finished_response | None, params, candidates)``.
    """
    return None, {"trace_id": trace_id}, _TRACES_GET_CANDIDATES


def _traces_search_dispatch(
    service: str, operation: str, query: str, limit: int
) -> tuple[str | None, dict[str, Any], Any]:
    """``graph_traces action='search'`` — the KG-native sink first, else the probe."""
    params = _drop_empty(service=service, operation=operation, query=query)
    params["limit"] = int(limit)
    return None, params, _TRACES_SEARCH_CANDIDATES


def register_trace_tools(mcp, tools: ToolTable) -> None:
    """Register the trace_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # graph_promql — observability: PromQL metric queries
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_promql",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — query the engine's observability metrics with PromQL. "
            "action='instant' (a single evaluation at 'time', default now) or 'range' "
            "(over start..end at 'step'). Extra engine kwargs via params_json. Degrades "
            "cleanly when the engine build has no metrics/PromQL surface."
        ),
        tags=["arbora", "engine", "observability", "metrics"],
    )
    def graph_promql(
        query: str = Field(description="A PromQL expression."),
        action: str = Field(default="instant", description="instant | range"),
        time: str = Field(
            default="", description="Evaluation time (instant), RFC3339/unix."
        ),
        start: str = Field(default="", description="Range start (range)."),
        end: str = Field(default="", description="Range end (range)."),
        step: str = Field(default="", description="Range step, e.g. '30s' (range)."),
        params_json: str = Field(
            default="{}", description="JSON object of extra engine kwargs."
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine PromQL surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        try:
            extra = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="promql", code="invalid_request")
        if not isinstance(extra, dict):
            return json.dumps(
                {"surface": "promql", "error": "params_json must decode to an object"}
            )
        if action == "range":
            params = _drop_empty(query=query, start=start, end=end, step=step)
            candidates = _PROMQL_RANGE_CANDIDATES
        elif action == "instant":
            params = _drop_empty(query=query, time=time)
            candidates = _PROMQL_INSTANT_CANDIDATES
        else:
            return json.dumps(
                {"surface": "promql", "error": f"unknown action {action!r}"}
            )
        params.update(extra)
        return _invoke_promql(
            action=action,
            graph=graph,
            candidates=candidates,
            params=params,
        )

    tools["graph_promql"] = graph_promql

    tools.bind_route("graph_promql", "/graph/promql")

    # ══════════════════════════════════════════════════════════════════
    # graph_traces — observability: distributed-trace search / fetch
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_traces",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — search or fetch distributed traces. "
            "action='search' (filter by 'service'/'operation'/free-form 'query', capped by "
            "'limit'), 'get' (a single 'trace_id'), or 'waterfall' (a single 'trace_id''s full "
            "Span/Generation subgraph, flattened for the trace-waterfall MCP App). All three "
            "prefer the always-on KG-native trace sink when installed, falling back to the raw "
            "engine observability surface only for 'search'/'get' when no sink is installed. "
            "Extra engine kwargs via params_json (engine-surface fallback only). Degrades "
            "cleanly when neither a sink nor a matching engine surface is available."
        ),
        tags=["arbora", "engine", "observability", "traces"],
    )
    def graph_traces(
        action: str = Field(default="search", description="search | get | waterfall"),
        trace_id: str = Field(
            default="", description="Trace id (action='get'/'waterfall')."
        ),
        service: str = Field(default="", description="Service name filter (search)."),
        operation: str = Field(
            default="", description="Operation/span name filter (search)."
        ),
        query: str = Field(
            default="", description="Free-form filter expression (search)."
        ),
        limit: int = Field(default=20, description="Max traces to return (search)."),
        params_json: str = Field(
            default="{}", description="JSON object of extra engine kwargs."
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine trace surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        extra, rejection = _decode_json_object(params_json, "traces")
        if rejection is not None:
            return rejection
        if action == "get" and not trace_id:
            return json.dumps({"surface": "traces", "error": "trace_id required"})
        if action == "get":
            done, params, candidates = _traces_get_dispatch(trace_id)
        elif action == "search":
            done, params, candidates = _traces_search_dispatch(
                service, operation, query, limit
            )
        else:
            return json.dumps(
                {"surface": "traces", "error": f"unknown action {action!r}"}
            )
        if done is not None:
            return done
        params.update(extra)
        return _invoke(
            surface="traces",
            action=action,
            graph=graph,
            candidates=candidates,
            params=params,
        )

    tools["graph_traces"] = graph_traces

    tools.bind_route("graph_traces", "/graph/traces")

    # ══════════════════════════════════════════════════════════════════
    # graph_logs — observability: log stream query (D-W6-ISO-2)
    # ══════════════════════════════════════════════════════════════════
    # Register-visible history: the webui Live Dashboards' Logs panel POSTed
    # to ``/graph/logs`` and got a hard HTTP 405 -- no ``graph_logs`` tool (and
    # therefore no ``/graph/logs`` route) had ever been registered at all, so
    # the path was genuinely unregistered, not merely misconfigured. Adding it
    # here, mirroring graph_promql/graph_traces exactly, makes ``/graph/logs``
    # a REAL POST route (the 405 -> a normal 200 that degrades to
    # ``{"surface": "logs", ...}`` when the engine build has no log-query
    # surface yet -- see ``_LOGS_QUERY_CANDIDATES`` above). The engine's own
    # log surface (``MEDULLA_OBS_ADDR``) today only ever INGESTS
    # (``POST /v1/logs``); no query RPC exists on the wire client yet, so this
    # degrades cleanly like graph_promql/graph_traces already do for a build
    # without their surface -- never fabricates a log line.
    @mcp.tool(
        name="graph_logs",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — query the engine's log stream. "
            "action='query' (default): filter by 'stream'/free-form 'query' over "
            "start..end, capped by 'limit'. Extra engine kwargs via params_json. "
            "Degrades cleanly when the engine build has no log-query surface."
        ),
        tags=["arbora", "engine", "observability", "logs"],
    )
    def graph_logs(
        action: str = Field(default="query", description="query"),
        stream: str = Field(default="", description="Log stream/source filter."),
        query: str = Field(default="", description="Free-form filter expression."),
        start: str = Field(default="", description="Range start, RFC3339/unix."),
        end: str = Field(default="", description="Range end, RFC3339/unix."),
        limit: int = Field(default=200, description="Max log lines to return."),
        params_json: str = Field(
            default="{}", description="JSON object of extra engine kwargs."
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine log-query surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        try:
            extra = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="logs", code="invalid_request")
        if not isinstance(extra, dict):
            return json.dumps(
                {"surface": "logs", "error": "params_json must decode to an object"}
            )
        if action != "query":
            return json.dumps(
                {"surface": "logs", "error": f"unknown action {action!r}"}
            )
        params = _drop_empty(stream=stream, query=query, start=start, end=end)
        params["limit"] = int(limit)
        params.update(extra)
        return _invoke(
            surface="logs",
            action=action,
            graph=graph,
            candidates=_LOGS_QUERY_CANDIDATES,
            params=params,
        )

    tools["graph_logs"] = graph_logs

    tools.bind_route("graph_logs", "/graph/logs")
