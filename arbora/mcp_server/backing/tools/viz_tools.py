"""The chart engine-surface tool (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import Field

from arbora.mcp_server import runtime
from arbora.mcp_server.backing.engine_surface_types import (
    ChartRenderRequest,
)
from arbora.mcp_server.backing.tools.engine_surface_common import (
    _client,
    _degraded,
    _json_default,
    _surface_error,
)
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


# Marks a flat SQL result set can drive directly (x/y[/color][/size] encodings
# over ordinary columns). 'graph' (node-link) needs a node/edge dataset shape a
# single result set doesn't carry — that stays 'export_chart'-only.
_VIZ_QUERY_MARKS: frozenset[str] = frozenset(
    {"line", "scatter", "bar", "area", "heatmap"}
)


# Safety cap on rows shipped inline over the wire for 'plot_from_query' — NOT
# the render budget (max_primitives/max_bytes, honored server-side by
# eg_viz_core::select_tier regardless of this value). A caller with a bigger
# result should aggregate/LIMIT in the query itself; this only bounds the
# request payload this tool will build.
_VIZ_MAX_INLINE_ROWS = 2_000_000


def _plot_spec(
    mark: str,
    data_ref: str,
    x_field: str,
    y_field: str,
    color_field: str,
    size_field: str,
    title: str,
) -> dict[str, Any]:
    """Build a minimal ``eg_viz_core::ViewSpec`` (as JSON) for one mark over
    ``data_ref``'s x/y[/color][/size] encodings. Mirrors the field-name/shape
    convention ``medulla.client.VizClient.render`` documents."""
    encodings: dict[str, Any] = {
        "x": {"field": x_field},
        "y": {"field": y_field},
    }
    if color_field:
        encodings["color"] = {"field": color_field}
    if size_field:
        encodings["size"] = {"field": size_field}
    spec: dict[str, Any] = {
        "version": 1,
        "marks": [{"kind": mark, "data_ref": data_ref, "encodings": encodings}],
    }
    if title:
        spec["title"] = title
    return spec


def _complete_row(row: Any, fields: list[str]) -> dict[str, Any] | None:
    """The row's value for EVERY field, or ``None`` when any one is absent.

    Never fabricates: a missing key or a SQL NULL disqualifies the whole row
    rather than being backfilled with an invented ``0.0``/``""``.
    """
    if not isinstance(row, dict):
        return None
    values: dict[str, Any] = {}
    for f in fields:
        v = row.get(f)
        if v is None:
            return None
        values[f] = v
    return values


def _keep_complete_rows(
    rows: list[Any], fields: list[str], row_limit: int
) -> list[dict[str, Any]]:
    """The first ``row_limit`` rows carrying a value for every one of ``fields``."""
    kept: list[dict[str, Any]] = []
    for row in rows:
        values = _complete_row(row, fields)
        if values is None:
            continue
        kept.append(values)
        if len(kept) >= row_limit:
            break
    return kept


def _encode_columns(kept: list[dict[str, Any]], fields: list[str]) -> dict[str, Any]:
    """Encode surviving rows as ``VizColumnValues`` columns.

    A field is ``F64`` iff every surviving value in it is a real number
    (excluding bool, which JSON/SQL both can return where a caller might expect
    a numeric flag but this DTO has no bool column type); otherwise the whole
    field is ``Utf8`` — matching ``eg_types::viz::VizColumnValues``'s only two
    variants.
    """
    columns: dict[str, Any] = {}
    for f in fields:
        col_values = [row[f] for row in kept]
        if all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in col_values
        ):
            columns[f] = {"F64": [float(v) for v in col_values]}
        else:
            columns[f] = {"Utf8": [str(v) for v in col_values]}
    return columns


def _rows_to_inline_columns(
    rows: list[Any], fields: list[str], row_limit: int
) -> tuple[dict[str, Any] | None, int, int]:
    """Shape ``client.query.sql`` result rows into a
    ``VizDatasetSource::InlineColumns`` ``columns`` map (``{field: {"F64": [...]}
    | {"Utf8": [...]}}``).

    Never fabricates a value: a row missing any one of ``fields`` (including a
    SQL NULL) is DROPPED, never backfilled with an invented value —
    ``VizColumnValues`` (the wire DTO) carries no null bit, so a dropped row
    is the honest choice over inventing 0.0/"" for a value the query never
    produced. Returns
    ``(None, 0, rows_returned)`` when zero rows survive that filter (the
    caller turns this into an explicit ``unavailable`` response, never an
    empty-but-"successful" chart) — otherwise ``(columns, rows_used,
    rows_returned)``.
    """
    if not isinstance(rows, list):
        return None, 0, 0
    rows_returned = len(rows)
    kept = _keep_complete_rows(rows, fields, row_limit)
    if not kept:
        return None, 0, rows_returned
    return _encode_columns(kept, fields), len(kept), rows_returned


def _render_chart(
    viz_client: Any, *, surface: str, action: str, request: ChartRenderRequest
) -> str:
    """Call ``client.viz.render`` and wrap the result in the same
    ``{"surface", "action", "result"}`` envelope every other tool in this
    module uses; PNG/SVG/PDF bytes survive JSON encoding via ``_json_default``
    (base64, the SAME convention every other bytes-carrying tool result uses)."""
    try:
        result = viz_client.render(
            request.spec,
            request.dataset,
            width_px=request.width_px,
            height_px=request.height_px,
            format=request.format,
            max_primitives=request.max_primitives,
            max_bytes=request.max_bytes,
            dataset_ref=request.dataset_ref,
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "surface engine errors as data", exc_info=True
        )
        return _surface_error(exc, surface=surface, action=action, code="render_failed")
    return json.dumps(
        {"surface": surface, "action": action, "result": result},
        default=_json_default,
    )


def _spec_mark(spec_json: str) -> Any:
    """The first mark's ``kind`` from a ViewSpec JSON string, or ``None``.

    Never raises: an absent, unparsable or unexpectedly shaped spec simply
    yields ``None`` (the summary then says "a chart" rather than naming a mark).
    """
    if not spec_json.strip():
        return None
    try:
        spec = json.loads(spec_json)
        marks = spec.get("marks") if isinstance(spec, dict) else None
        if isinstance(marks, list) and marks and isinstance(marks[0], dict):
            return marks[0].get("kind")
    except (TypeError, ValueError):
        return None
    return None


def _chart_summary_parts(vr: dict[str, Any], mark: Any) -> list[str]:
    """The plain-language sentences describing what a ``ViewResult`` reports.

    States only what the payload itself carries, and is explicit that a
    non-exact render is an approximation the caller must not read as precise.
    """
    row_count = vr.get("row_count")
    what = f"a {mark} chart" if mark else "a chart"
    parts = [
        f"Rendered {what} from {row_count if row_count is not None else 'an unknown number of'} row(s)."
    ]
    lod_tier = vr.get("lod_tier")
    if lod_tier is not None:
        parts.append(f"LOD tier: {lod_tier}.")
    exact = vr.get("exact")
    if exact is True:
        parts.append(
            "Exact — every row is individually represented; no reduction was applied."
        )
    elif exact is False:
        reduction = vr.get("reduction")
        parts.append(
            f"Approximated (reduction: {reduction or 'unknown'}) — not every row is individually "
            "represented; treat values read off this chart as visual trends, not precise per-row facts."
        )
    wall_time_ms = vr.get("wall_time_ms")
    if wall_time_ms is not None:
        parts.append(f"Rendered in {wall_time_ms} ms.")
    return parts


def _describe_chart(view_result_json: str, spec_json: str) -> str:
    """A pure, LLM-facing summary of a ``ViewResult`` already in hand — no
    engine call. States only what ``view_result`` itself reports (row count,
    LOD tier, exact-vs-approximated, timing/size) — it never infers a fact the
    payload doesn't carry, and it is explicit that a non-exact render is an
    approximation the caller should not treat as a precise value."""
    if not view_result_json.strip():
        return json.dumps(
            {
                "surface": "viz",
                "action": "describe_chart",
                "error": "view_result_json is required",
            }
        )
    try:
        vr = json.loads(view_result_json)
    except (TypeError, ValueError) as exc:
        return _surface_error(
            exc, surface="viz", action="describe_chart", code="invalid_request"
        )
    if not isinstance(vr, dict):
        return json.dumps(
            {
                "surface": "viz",
                "action": "describe_chart",
                "error": "view_result_json must decode to an object",
            }
        )
    mark = _spec_mark(spec_json)
    return json.dumps(
        {
            "surface": "viz",
            "action": "describe_chart",
            "result": {
                "summary": " ".join(_chart_summary_parts(vr, mark)),
                "mark": mark,
                "row_count": vr.get("row_count"),
                "lod_tier": vr.get("lod_tier"),
                "exact": vr.get("exact"),
                "reduction": vr.get("reduction"),
            },
        }
    )


def _viz_capability_matrix(viz_client: Any, action: str) -> str:
    """``action='capability_matrix'`` — which mark x surface pairs are real today."""
    try:
        result = viz_client.capability_matrix()
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "surface engine errors as data", exc_info=True
        )
        return _surface_error(exc, surface="viz", action=action)
    return json.dumps(
        {"surface": "viz", "action": action, "result": result},
        default=_json_default,
    )


def _viz_export_chart(
    viz_client: Any,
    action: str,
    spec_json: str,
    dataset_json: str,
    render_opts: dict[str, Any],
) -> str:
    """``action='export_chart'`` — a thin already-built ViewSpec/dataset pass-through."""
    try:
        spec = json.loads(spec_json) if spec_json else {}
        dataset = json.loads(dataset_json) if dataset_json else {}
    except (TypeError, ValueError) as exc:
        return _surface_error(exc, surface="viz", action=action, code="invalid_request")
    if not spec or not dataset:
        return json.dumps(
            {
                "surface": "viz",
                "action": action,
                "error": "spec_json and dataset_json are both required",
            }
        )
    return _render_chart(
        viz_client,
        surface="viz",
        action=action,
        request=ChartRenderRequest(spec=spec, dataset=dataset, **render_opts),
    )


def _viz_plot_query_guard(action: str, plot: dict[str, Any]) -> str | None:
    """Reject a ``plot_from_query`` request the flat-result path cannot serve.

    Returns the ready-to-send rejection, or ``None`` when the request is
    serviceable. The order of the two checks is load-bearing: a request that is
    both incomplete AND asks for a non-flat mark still reports the missing
    fields first, exactly as before.
    """
    if (
        not plot["query"].strip()
        or not plot["x_field"].strip()
        or not plot["y_field"].strip()
    ):
        return json.dumps(
            {
                "surface": "viz",
                "action": action,
                "error": "query, x_field, and y_field are all required",
            }
        )
    mark = plot["mark"]
    if mark not in _VIZ_QUERY_MARKS:
        return json.dumps(
            {
                "surface": "viz",
                "action": action,
                "error": f"mark {mark!r} is not usable from a flat query result; "
                f"use one of {sorted(_VIZ_QUERY_MARKS)} or action='export_chart' "
                "with an explicit graph/node-edge dataset",
            }
        )
    return None


def _viz_query_rows(client: Any, action: str, query: str) -> tuple[Any, str | None]:
    """Run the read-only SQL behind ``plot_from_query``.

    Returns ``(rows, None)``, or ``(None, error_json)`` when the query surface is
    absent or the query itself failed.
    """
    query_client = getattr(client, "query", None)
    if query_client is None or not hasattr(query_client, "sql"):
        return None, _degraded("viz", action, ["client.query.sql"])
    try:
        return query_client.sql(query), None
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "surface engine errors as data", exc_info=True
        )
        return None, _surface_error(
            exc, surface="viz", action=action, code="query_failed"
        )


def _viz_with_row_counts(resp: str, rows_returned: int, rows_used: int) -> str:
    """Fold ``plot_from_query``'s row accounting into a successful render envelope."""
    try:
        payload = json.loads(resp)
    except (TypeError, ValueError):
        return resp
    if "result" in payload:
        payload["rows_returned"] = rows_returned
        payload["rows_rendered"] = rows_used
    return json.dumps(payload, default=_json_default)


def _viz_plot_from_query(
    client: Any,
    viz_client: Any,
    action: str,
    plot: dict[str, Any],
    render_opts: dict[str, Any],
) -> str:
    """``action='plot_from_query'`` — run the SQL, shape it, render it.

    Answers an explicit ``{'unavailable': true}`` rather than an empty chart
    when no row carries every encoded field.
    """
    guard = _viz_plot_query_guard(action, plot)
    if guard is not None:
        return guard
    rows, error = _viz_query_rows(client, action, plot["query"])
    if error is not None:
        return error
    fields = [
        f
        for f in (
            plot["x_field"],
            plot["y_field"],
            plot["color_field"],
            plot["size_field"],
        )
        if f
    ]
    effective_row_limit = min(max(1, plot["row_limit"]), _VIZ_MAX_INLINE_ROWS)
    columns, rows_used, rows_returned = _rows_to_inline_columns(
        rows, fields, effective_row_limit
    )
    if columns is None:
        return json.dumps(
            {
                "surface": "viz",
                "action": action,
                "unavailable": True,
                "reason": (
                    f"query returned {rows_returned} row(s); 0 had every one of "
                    f"{fields} present, so there is nothing to render — this is "
                    "reported as unavailable, not rendered as an empty chart"
                ),
            }
        )
    spec = _plot_spec(
        plot["mark"],
        render_opts["dataset_ref"],
        plot["x_field"],
        plot["y_field"],
        plot["color_field"],
        plot["size_field"],
        plot["title"],
    )
    resp = _render_chart(
        viz_client,
        surface="viz",
        action=action,
        request=ChartRenderRequest(
            spec=spec,
            dataset={"InlineColumns": {"columns": columns}},
            **render_opts,
        ),
    )
    return _viz_with_row_counts(resp, rows_returned, rows_used)


def register_viz_tools(mcp, tools: ToolTable) -> None:
    """Register the viz_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # graph_viz — GOC-88 (D-VZ-1 lane V5): agent-facing native visualization
    # ══════════════════════════════════════════════════════════════════
    # The engine ships a working render surface (`Method::Viz`, D-VZ-1 V0/V1/
    # V3a/V4-lite/V6-lite: eg-viz-core/eg-viz-columnstore/eg-viz-export, the
    # ColumnStore/LOD pipeline, and `medulla.client.VizClient` on the
    # Python side) but NO agent-facing tool called it — this is that tool.
    # Action-routed exactly like `graph_promql`/`graph_gis` above (one MCP
    # tool + its REST twin from one core, per `graph_analyze`'s convention):
    #
    # * 'capability_matrix' — the (mark, surface) support matrix.
    # * 'export_chart'      — render an already-built ViewSpec against an
    #   already-shaped dataset (InlineColumns / SyntheticScatterClusters /
    #   SyntheticGraph, verbatim `VizDatasetSource` JSON) — a thin pass-
    #   through to `client.viz.render`, for a caller that already has both.
    # * 'plot_from_query'   — the common path: run a read-only SQL query
    #   against the connected graph (`client.query.sql`), shape the result
    #   rows into an InlineColumns dataset for the requested field(s), build
    #   a ViewSpec for the requested mark, and render it. NEVER fabricates —
    #   a query that returns no rows (or no rows with every requested field
    #   present) answers `{"unavailable": true, "reason": ...}`, not an
    #   empty-but-successful chart.
    # * 'describe_chart'    — a pure, LLM-facing summary of a `ViewResult`
    #   already in hand (own no engine call): what mark/tier was used, and
    #   whether the render is exact or an approximation the caller should
    #   not treat as a hard fact.
    #
    # Only marks that map onto a flat query result (line/scatter/bar/area/
    # heatmap) are supported by 'plot_from_query'; 'graph' marks need a
    # node/edge dataset shape 'plot_from_query' does not attempt to infer
    # from a single SQL result set — use 'export_chart' with an explicit
    # dataset for those (or the engine's own SyntheticGraph demo path).
    @mcp.tool(
        name="graph_viz",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — GOC-88/D-VZ-1 V5: render native "
            "charts through the engine's LOD ColumnStore/export pipeline (Method::Viz) "
            "— no matplotlib, no per-view chart code. action='plot_from_query' (the "
            "common path: 'query' [+'mark','x_field','y_field',+'color_field',"
            "+'size_field',+'title'] — runs the SQL query against the connected graph "
            "and renders the result; answers {'unavailable': true} rather than an "
            "empty chart when the query returns no usable rows), 'export_chart' "
            "('spec_json' + 'dataset_json' — an already-built ViewSpec/VizDatasetSource "
            "pair, thin pass-through), 'describe_chart' ('view_result_json' [+'spec_json'] "
            "— a plain-language, LLM-facing summary of what was actually drawn: mark, "
            "LOD tier, exact-vs-approximated, row count — no engine call), or "
            "'capability_matrix' (which mark x surface pairs are real today). "
            "'format' is png|svg|pdf; 'width_px'/'height_px' bound the canvas; "
            "'max_primitives'/'max_bytes' bound the render budget — LOD selection is "
            "bounded by these, never by row count. Degrades cleanly ({'degraded': true}) "
            "when the engine build has no viz surface (feature 'viz-static-export')."
        ),
        tags=["arbora", "engine", "visualization", "chart", "viz"],
    )
    def graph_viz(
        action: str = Field(
            default="capability_matrix",
            description="capability_matrix | export_chart | plot_from_query | describe_chart",
        ),
        query: str = Field(
            default="", description="Read-only SQL query (action='plot_from_query')."
        ),
        mark: str = Field(
            default="scatter",
            description="line | scatter | bar | area | heatmap (action='plot_from_query').",
        ),
        x_field: str = Field(
            default="",
            description="Result column bound to x (action='plot_from_query').",
        ),
        y_field: str = Field(
            default="",
            description="Result column bound to y (action='plot_from_query').",
        ),
        color_field: str = Field(
            default="", description="Optional result column bound to color."
        ),
        size_field: str = Field(
            default="", description="Optional result column bound to size."
        ),
        title: str = Field(default="", description="Optional chart title."),
        spec_json: str = Field(
            default="",
            description="A full eg_viz_core::ViewSpec as JSON (action='export_chart').",
        ),
        dataset_json: str = Field(
            default="",
            description="A eg_types::viz::VizDatasetSource as JSON, externally tagged "
            "e.g. {'InlineColumns': {...}} (action='export_chart').",
        ),
        view_result_json: str = Field(
            default="",
            description="A prior render's 'view_result' as JSON (action='describe_chart').",
        ),
        format: str = Field(default="png", description="png | svg | pdf."),
        width_px: int = Field(default=900, description="Canvas width in pixels."),
        height_px: int = Field(default=560, description="Canvas height in pixels."),
        max_primitives: int = Field(
            default=200_000, description="Frame primitive budget (bounds LOD tier)."
        ),
        max_bytes: int = Field(
            default=50_000_000, description="Frame byte budget (bounds LOD tier)."
        ),
        row_limit: int = Field(
            default=200_000,
            description="Max query rows fed inline into the render (action='plot_from_query'); "
            "the query itself should aggregate/LIMIT for anything larger — this is a "
            "safety cap on the wire payload, not the render budget (that's "
            "max_primitives/max_bytes, honored server-side regardless of row_limit).",
        ),
        dataset_ref: str = Field(
            default="graph_viz", description="Dataset id for the render."
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine native-visualization surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        surface = "viz"
        if action == "describe_chart":
            return _describe_chart(view_result_json, spec_json)
        try:
            client = _client(graph)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "engine down is a normal degrade", exc_info=True
            )
            return _surface_error(
                exc, surface=surface, action=action, code="dependency_unavailable"
            )
        viz_client = getattr(client, "viz", None)
        if viz_client is None:
            return _degraded(surface, action, ["client.viz"])
        render_opts: dict[str, Any] = {
            "width_px": width_px,
            "height_px": height_px,
            "format": format,
            "max_primitives": max_primitives,
            "max_bytes": max_bytes,
            "dataset_ref": dataset_ref,
        }
        if action == "capability_matrix":
            return _viz_capability_matrix(viz_client, action)
        if action == "export_chart":
            return _viz_export_chart(
                viz_client, action, spec_json, dataset_json, render_opts
            )
        if action == "plot_from_query":
            return _viz_plot_from_query(
                client,
                viz_client,
                action,
                {
                    "query": query,
                    "mark": mark,
                    "x_field": x_field,
                    "y_field": y_field,
                    "color_field": color_field,
                    "size_field": size_field,
                    "title": title,
                    "row_limit": row_limit,
                },
                render_opts,
            )
        return json.dumps(
            {
                "surface": surface,
                "action": action,
                "error": f"unknown action {action!r}",
            }
        )

    tools["graph_viz"] = graph_viz

    runtime.ACTION_TOOL_ROUTES["graph_viz"] = "/graph/viz"
