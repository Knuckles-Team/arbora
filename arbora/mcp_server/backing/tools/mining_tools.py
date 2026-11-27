"""The mining engine-surface tools: process mining and deep mining (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.domain.mcp_server.backing.context_helpers import run_coro as _run_coro
from arbora.api.errors import not_served
from arbora.fleet_tool_call import call_fleet_tool, run_coroutine
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.tools._surface_selection import (
    conditional_tool_decorator,
    register_conditional_tools,
)
from arbora.mcp_server.backing.tools.engine_surface_common import (
    _decode_json_object,
    _invoke,
    _json_default,
    _surface_error,
)
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.identifiers import CYPHER_IDENTIFIER_RE

logger = logging.getLogger(__name__)


# ── graph_mine action manifest (CONCEPT:MEDULLA-KG.mining.frequent-itemset-mining) ─
def _mining_actions() -> frozenset[str]:
    """The valid ``graph_mine`` actions.

    Reuses ``engine_tools.ENGINE_DOMAINS['mining']`` — the SAME
    client-introspected manifest (``inspect.getmembers`` over ``MiningClient``,
    mirroring ``engine_tools._discover_domains``) that already drives the
    granular ``engine_mining`` 1:1 tool — as the one source of truth, so this
    surface can never drift out of sync with the real client again
    (CONCEPT:MERISTEM-KG.compute.engine-surface-manifest). Empty only when the
    ``medulla`` client itself can't be imported (an environment/build
    issue unrelated to action naming); callers then skip the allow-list check
    below and fall through to the normal dispatch/degrade path.
    """
    from arbora.mcp_server.backing.tools import engine_tools

    return frozenset(engine_tools.ENGINE_DOMAINS.get("mining", ()))


#: Guessable alternate spellings that would otherwise silently miss their real
#: ``MiningClient`` attribute and fall through to a misleading "whole surface
#: degraded" payload — ``entity_resolution`` (the family's doc/CONCEPT name) is
#: actually bound as ``entity_resolve``, and ``process_mining`` as ``process``.
#: Looked up AFTER the hyphen->underscore normalization below, so the
#: hyphenated spellings (``entity-resolution``, ``process-mining``) resolve too.
_MINING_ACTION_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "entity_resolution": "entity_resolve",
        "process_mining": "process",
    }
)


# ══════════════════════════════════════════════════════════════════
# graph_mine_deep — Phase-6 heavy-dep delegation to data-science-mcp
# ══════════════════════════════════════════════════════════════════
# CONCEPT:MERISTEM-KG.mining.dsm-forecast-delegation — the data-mining plan's Phase 6: the engine
# core stays pure-Rust (never torch/GPU); the deep-learning / heavy-Python family
# (LSTM/RNN sequence forecasting, MLP/deep classifiers, autoencoders, an XGBoost-
# family boosting classifier) is ORCHESTRATED over MCP to ``agents/data-science-mcp``
# — this module ships features out, that service trains/infers, and the result
# folds back into the KG as typed nodes (CONCEPT:MERISTEM-KG.mining.foldback-typed-nodes). Word/text
# embeddings are deliberately NOT re-homed here — they already live on the remote
# vLLM embedder (``core/embedding_utilities``); the 'embed' action below embeds
# arbitrary NUMERIC feature rows via a small neural autoencoder, a distinct
# capability from text embedding.
_DSM_SERVER_NAME = "data-science-mcp"


_DSM_TOOL_NAME = "deep_train_predict"


#: One delegated ``graph_mine_deep`` action → the data-science-mcp
#: ``deep_train_predict`` algo it maps to.
_DEEP_ALGO_BY_ACTION: Mapping[str, str] = MappingProxyType(
    {
        "deep_forecast": "lstm_forecast",
        "deep_classify": "mlp_classify",
        "autoencoder_anomaly": "autoencoder_anomaly",
        "xgboost": "histgbm_classify",
        "embed": "autoencoder_embed",
    }
)


#: KG node type a delegated action's result materializes as when writeback=true.
_DEEP_NODE_TYPE: Mapping[str, str] = MappingProxyType(
    {
        "deep_forecast": "Forecast",
        "deep_classify": "Classification",
        "autoencoder_anomaly": "Anomaly",
        "xgboost": "Classification",
        "embed": "Embedding",
    }
)


def _feature_node_label(source: dict[str, Any]) -> str:
    """The validated Cypher-safe node label of a ``{node_label, fields, limit}`` spec."""
    node_label = source.get("node_label")
    if not isinstance(node_label, str) or not CYPHER_IDENTIFIER_RE.fullmatch(
        node_label
    ):
        raise ValueError("source.node_label is required")
    return node_label


def _feature_fields(source: dict[str, Any]) -> list[str]:
    """The validated, Cypher-safe property names to project (1..64 of them)."""
    fields = source.get("fields") or []
    if (
        not isinstance(fields, list)
        or not fields
        or len(fields) > 64
        or any(
            not isinstance(field, str) or not CYPHER_IDENTIFIER_RE.fullmatch(field)
            for field in fields
        )
    ):
        raise ValueError("source.fields (a list of property names) is required")
    return fields


def _feature_limit(source: dict[str, Any]) -> int:
    """The validated row cap (1..10000); ``bool`` is rejected, not silently 0/1."""
    raw_limit = source.get("limit", 200)
    if isinstance(raw_limit, bool):
        raise ValueError("source.limit must be between 1 and 10000")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("source.limit must be between 1 and 10000") from exc
    if not 1 <= limit <= 10_000:
        raise ValueError("source.limit must be between 1 and 10000")
    return limit


def _feature_query_rows(tools: ToolTable, cypher: str, graph: str) -> list[Any]:
    """Run one read-only projection through ``graph_query`` and validate its shape."""
    raw = _run_coro(
        runtime._execute_tool(
            tools,
            "graph_query",
            query=cypher,
            params="{}",
            scope="local",
            graph=graph or "",
        )
    )
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(payload, dict) and "error" in payload:
        raise RuntimeError("graph feature query failed")
    if not isinstance(payload, list):
        raise RuntimeError(
            f"unexpected graph_query result shape: {type(payload).__name__}"
        )
    return payload


def _gather_kg_feature_rows(
    tools: ToolTable, source: dict[str, Any], graph: str
) -> tuple[list[str], list[list[float]]]:
    """Gather a feature-row RowSet from the KG for a ``{node_label, fields, limit}`` source spec.

    Runs one read-only Cypher projection through the existing ``graph_query`` tool
    (compute-near-data — no bespoke second engine client) and returns
    ``(node_ids, rows)`` so a caller can ship ``rows`` to data-science-mcp and fold
    the result back onto the SAME ``node_ids`` (CONCEPT:MERISTEM-KG.mining.dsm-forecast-delegation).

    The three validations run in their original order — label, then fields, then
    limit — so a multiply-invalid spec still reports the same first offence.
    """
    node_label = _feature_node_label(source)
    fields = _feature_fields(source)
    limit = _feature_limit(source)
    projections = ", ".join(f"n.{f} AS f{i}" for i, f in enumerate(fields))
    cypher = f"MATCH (n:{node_label}) RETURN n.id AS id, {projections} LIMIT {limit}"
    payload = _feature_query_rows(tools, cypher, graph)
    node_ids = [str(row.get("id")) for row in payload]
    rows = [
        [float(row.get(f"f{i}") or 0.0) for i in range(len(fields))] for row in payload
    ]
    return node_ids, rows


def _deep_write_node(
    tools: ToolTable, node_type: str, properties: dict[str, Any], graph: str
) -> str:
    """Materialize one delegated-deep-mining result row as a typed KG node
    (CONCEPT:MERISTEM-KG.mining.foldback-typed-nodes). Best-effort — never raises, since a foldback
    failure must not fail the already-completed delegated call. Returns the new node id."""
    node_id = f"{node_type.lower()}_dsm_{uuid.uuid4().hex}"
    try:
        _run_coro(
            runtime._execute_tool(
                tools,
                "graph_write",
                action="add_node",
                node_id=node_id,
                node_type=node_type,
                properties=json.dumps(properties, default=_json_default),
                target=graph or "",
            )
        )
    except Exception:  # writeback is best-effort
        logger.debug("deep-mining writeback failed", exc_info=True)
    return node_id


def _deep_write_edge(
    tools: ToolTable, source_id: str, target_id: str, rel_type: str, graph: str
) -> None:
    """Link a delegated-deep-mining node back to the KG node it was derived from
    (best-effort — see :func:`_deep_write_node`)."""
    try:
        _run_coro(
            runtime._execute_tool(
                tools,
                "graph_write",
                action="add_edge",
                source_id=source_id,
                target_id=target_id,
                rel_type=rel_type,
                target=graph or "",
            )
        )
    except Exception:  # writeback is best-effort
        logger.debug("deep-mining writeback failed", exc_info=True)


def _deep_series_params(
    tools: ToolTable, params: dict[str, Any], source: Any, graph: str
) -> tuple[dict[str, Any], list[str]]:
    """``deep_forecast`` — a 1-D series, gathered from the KG when not given directly."""
    node_ids: list[str] = []
    values = params.pop("values", None)
    if values is None and source:
        node_ids, rows = _gather_kg_feature_rows(tools, source, graph)
        values = [row[0] for row in rows]
    if not values:
        raise ValueError("provide 'values' (a 1-D series) or a 'source'")
    return {"values_json": json.dumps(values)}, node_ids


def _deep_supervised_params(
    tools: ToolTable, params: dict[str, Any], source: Any, graph: str
) -> tuple[dict[str, Any], list[str]]:
    """``deep_classify``/``xgboost`` — a labelled matrix (+ an optional predict set)."""
    node_ids: list[str] = []
    x = params.pop("x", None)
    y = params.pop("y", None)
    if x is None and source:
        node_ids, x = _gather_kg_feature_rows(tools, source, graph)
    if x is None or y is None:
        raise ValueError("provide 'x' + 'y', or a 'source' + 'y'")
    tool_params = {"x_json": json.dumps(x), "y_json": json.dumps(y)}
    x_predict = params.pop("x_predict", None)
    if x_predict is not None:
        tool_params["x_predict_json"] = json.dumps(x_predict)
    return tool_params, node_ids


def _deep_matrix_params(
    tools: ToolTable, params: dict[str, Any], source: Any, graph: str
) -> tuple[dict[str, Any], list[str]]:
    """``autoencoder_anomaly``/``embed`` — an unlabelled feature matrix."""
    node_ids: list[str] = []
    x = params.pop("x", None)
    if x is None and source:
        node_ids, x = _gather_kg_feature_rows(tools, source, graph)
    if x is None:
        raise ValueError("provide 'x' or a 'source'")
    return {"x_json": json.dumps(x)}, node_ids


def _prepare_deep_delegation(
    tools: ToolTable, action: str, params: dict[str, Any], graph: str
) -> tuple[dict[str, Any], list[str]]:
    """Marshal ``params`` into the ``deep_train_predict`` kwargs for ``action``.

    Gathers a KG RowSet via ``source`` when raw ``x``/``values`` are not given
    directly. Returns ``(tool_params, node_ids)`` — ``node_ids`` is only populated
    when a ``source`` was used (so the caller can fold results back onto them).
    """
    algo = _DEEP_ALGO_BY_ACTION[action]
    source = params.pop("source", None)
    if action == "deep_forecast":
        extra, node_ids = _deep_series_params(tools, params, source, graph)
    elif action in ("deep_classify", "xgboost"):
        extra, node_ids = _deep_supervised_params(tools, params, source, graph)
    else:  # autoencoder_anomaly, embed
        extra, node_ids = _deep_matrix_params(tools, params, source, graph)
    tool_params: dict[str, Any] = {"algo": algo, **extra}
    tool_params["params_json"] = json.dumps(params)
    return tool_params, node_ids


def _graph_mine_process_events(action: str, params: dict, graph: str) -> str | None:
    """graph_mine's 'process' action, raw-events object-centric projection path.

    Extracted verbatim from ``graph_mine`` (pure extract-method, no
    behaviour change). Returns ``None`` when this branch's guard condition
    does not match -- the caller must then try the next special case /
    fall through to the generic ``_invoke`` dispatch, exactly like the
    original's sequential ``if action == "process" and ...:`` chain.
    """
    if not (action == "process" and "events" in params):
        return None
    if "traces" in params:
        return json.dumps(
            {
                "surface": "mining",
                "action": action,
                "code": "invalid_request",
                "error": "provide either events or traces, not both",
            }
        )
    if params.get("writeback") is True:
        return json.dumps(
            {
                "surface": "mining",
                "action": action,
                "code": "lineage_required",
                "error": (
                    "object-centric event projection writeback is disabled "
                    "until ProcessModel writeback retains source-event lineage"
                ),
            }
        )
    raise not_served(
        "knowledge_graph.ingestion.event_log_adapter", "MEDULLA-REPO-INGEST-R651"
    )


def _deep_action_or_error(action: str) -> tuple[str, str | None]:
    """Normalize a ``graph_mine_deep`` action and reject an unknown one by name."""
    normalized = (action or "").strip().replace("-", "_") or "deep_forecast"
    if normalized not in _DEEP_ALGO_BY_ACTION:
        return normalized, json.dumps(
            {
                "surface": "mining_deep",
                "action": normalized,
                "error": f"unknown action {normalized!r}; choose one of "
                f"{sorted(_DEEP_ALGO_BY_ACTION)}",
            }
        )
    return normalized, None


def _deep_unavailable(action: str, error: str) -> str:
    """The structured 'delegated but unavailable' payload — never a crash."""
    return json.dumps(
        {
            "surface": "mining_deep",
            "action": action,
            "provider": _DSM_SERVER_NAME,
            "delegated": True,
            "available": False,
            "error": error,
        }
    )


def _deep_call_delegate(
    action: str, tool_params: dict[str, Any]
) -> tuple[Any, str | None]:
    """Call data-science-mcp once, synchronously, and normalize its response.

    Returns ``(raw_dict, None)`` or ``(None, error_json)``.

    BUG-7: the fleet tool-call decoder returns the text content verbatim — when the delegate's tool itself
    returns an already-JSON-encoded string (this repo's own tool convention:
    ``return json.dumps(...)``), ``.data`` IS that raw string, not the parsed
    object, so ``raw`` arrives here as a ``str`` even though the delegate answered
    normally. Align the parse here instead of failing on a shape mismatch that
    isn't a real outage.
    """
    try:
        raw = run_coroutine(
            call_fleet_tool(
                server=_DSM_SERVER_NAME,
                tool=_DSM_TOOL_NAME,
                params=tool_params,
                params_style="args",
            )
        )
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "the delegate being unreachable degrades cleanly", exc_info=True
        )
        return None, _surface_error(
            exc,
            surface="mining_deep",
            action=action,
            code="dependency_unavailable",
            context={"delegated": True, "available": False},
        )
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            pass  # genuinely not JSON — falls through to the shape error below
    if not isinstance(raw, dict):
        return None, _deep_unavailable(
            action,
            f"unexpected data-science-mcp response shape: {type(raw).__name__}",
        )
    if not raw.get("available", True):
        return None, _deep_unavailable(
            action,
            raw.get("error", "data-science-mcp reported the algo unavailable"),
        )
    return raw, None


def _deep_writeback(
    tools: ToolTable,
    action: str,
    result: dict[str, Any],
    node_ids: list[str],
    series_id: str,
    graph: str,
) -> list[str]:
    """Materialize a delegated result as typed KG nodes.

    CONCEPT:MERISTEM-KG.mining.foldback-typed-nodes — each row-level node is linked
    DEEP_RESULT_OF its source node when a ``source`` was used; a forecast is
    linked FORECAST_OF its ``series_id`` node when one was given.
    """
    node_type = _DEEP_NODE_TYPE[action]
    algo = _DEEP_ALGO_BY_ACTION[action]
    if action == "deep_forecast":
        props = {"provider": _DSM_SERVER_NAME, "algo": algo, **result}
        node_id = _deep_write_node(tools, node_type, props, graph)
        if series_id:
            _deep_write_edge(tools, node_id, series_id, "FORECAST_OF", graph)
        return [node_id]
    written: list[str] = []
    for i, row in enumerate(result.get("rows") or []):
        props = {"provider": _DSM_SERVER_NAME, "algo": algo, **row}
        node_id = _deep_write_node(tools, node_type, props, graph)
        if i < len(node_ids):
            _deep_write_edge(tools, node_id, node_ids[i], "DEEP_RESULT_OF", graph)
        written.append(node_id)
    return written


def register_mining_tools(
    mcp, tools: ToolTable, *, include_unserved_mining: bool = False
) -> None:
    """Register the mining_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # graph_mine — data-mining surface (CONCEPT:MEDULLA-KG.mining.frequent-itemset-mining)
    # ══════════════════════════════════════════════════════════════════
    @conditional_tool_decorator(
        mcp,
        name="graph_mine",
        excluded_names=frozenset({"graph_mine", "graph_mine_deep"}),
        include_excluded=include_unserved_mining,
        description=(
            "CONCEPT:MEDULLA-KG.mining.frequent-itemset-mining — the unified data-mining surface "
            "over the engine, compute-near-data (mining runs where the graph lives). "
            "Actions (18): 'associate' (association rules), 'cluster' (clustering), "
            "'anomaly' (outlier detection), 'classify_fit'/'classify_predict' "
            "(classification), 'reduce' (dimensionality reduction), 'sequence' "
            "(sequential-pattern mining), 'forecast' (classical time-series forecasting), "
            "'text' (TF-IDF / topic modeling), 'subgraph' (frequent-subgraph mining / "
            "motif counting — mines the RESIDENT GRAPH's own topology, no input rows), "
            "'entity_resolve' (record linkage / entity resolution; alias "
            "'entity_resolution'), 'causal_impact' (interrupted-time-series / "
            "difference-in-differences causal effect estimation), 'process' (process "
            "mining — directly-follows graph + alpha-algorithm footprint; alias "
            "'process_mining'), 'root_cause' (root-cause propagation over a dependency "
            "graph), 'risk_propagation' (seeded personalized-PageRank risk propagation), "
            "'ontology_gap' (ontology completeness-gap detection), 'retrieval_quality' "
            "(precision@k / recall@k / MRR over retrieval traces), 'community' (Louvain / "
            "label-propagation community detection with epistemic writeback). An "
            "unrecognized action returns a clear error listing every valid action "
            "(introspected from the connected MiningClient) rather than silently "
            "degrading as if the whole mining surface were unavailable. "
            "• associate — frequent-itemset + rules (Apriori/FP-Growth/Eclat; support, "
            "confidence, lift). Provide 'transactions' (baskets of item labels) OR a "
            "graph-derived 'source' {node_label, direction(out|in|any), "
            "item_field(label|prop:<key>), relation, limit}. writeback ⇒ :AssociationRule nodes. "
            "• cluster — DBSCAN(default)/hierarchical/gmm/kmedoids over 'features' (a row "
            "matrix) OR a vector 'source' {node_label, limit} (the stored embeddings of "
            "those nodes — cross-modal 'cluster the vectors of these nodes'). Params: "
            "eps/min_pts (dbscan), k, linkage(single|complete|average), max_iter, seed. "
            "writeback ⇒ :Cluster nodes linked to members. Returns "
            "{clusters:[{cluster_id,members,centroid,score}], labels, ...} (gmm adds "
            "responsibilities). "
            "• anomaly — zscore(default)/isoforest/lof/ocsvm over 'features', a 1-D 'values' "
            "series (tsdb RCA), OR a vector 'source'. Params: k(lof), n_trees/sample_size/seed "
            "(isoforest), nu/kernel(rbf|linear)/gamma (ocsvm), threshold. writeback ⇒ "
            ":Anomaly nodes linked to their source. Returns {rows:[{id,anomaly_score,"
            "is_anomaly}], n_anomalies, threshold, ...}. "
            "• classify_fit — PREDICTIVE fit → model blob: gaussiannb(default)/multinomialnb/"
            "knn/logistic/svc over 'x' (rows) OR a vector 'source', plus integer 'y' labels. "
            "Params: k(knn), alpha(mnb), lr/epochs/l2(logistic), c(svc). Returns {model, "
            "classes, ...} — pass 'model' to classify_predict. "
            "• classify_predict — apply a fitted 'model' to 'x' OR a vector 'source' "
            "(cross-modal 'classify these nodes by their embeddings'). writeback ⇒ "
            ":Classification nodes linked to source. Returns {rows:[{id,label,proba}], classes}. "
            "• reduce — DESCRIPTIVE row transform: svd(default)/lda(supervised — needs "
            "'labels')/umap/tsne over 'x' OR a vector 'source' (reduce node vectors for the "
            "graphviz). Params: n_components, n_neighbors/min_dist(umap), perplexity/lr(tsne), "
            "epochs, seed. writeback ⇒ :Embedding2D nodes. Returns {rows:[{id,coords}], "
            "n_components, ...} (svd adds singular_values). UMAP/t-SNE are approximate, small-N. "
            "• sequence — frequent ORDERED subsequences: prefixspan(default)/gsp (both agree) "
            "over 'sequences' (time-ordered lists of item labels, repeats allowed) OR a "
            "graph-derived 'source' {node_label, direction(out|in|any), item_field, relation, "
            "limit} (each node's chronological neighbor history becomes one sequence — "
            "'what reliably follows what'). Params: min_support. writeback ⇒ :SequentialPattern "
            "nodes linked to their item nodes. Returns {patterns:[{items,support,count}], "
            "n_sequences, n_patterns, ...}. "
            "• forecast — classical forecasting over a 1-D 'values' series (a tsdb window "
            "handed in by the caller): arima(default, p/d/q — Hannan-Rissanen AR/MA)/"
            "holtwinters(alpha/beta/gamma, seasonal period — degrades to Holt linear-trend at "
            "period=0)/stl(classical decomposition + extrapolation, also returns trend/"
            "seasonal/residual). Params: horizon, confidence (band level). writeback ⇒ "
            ":Forecast node linked FORECAST_OF a resident node named 'series_id'. Returns "
            "{forecast, lower, upper, horizon, ...}. "
            "• text — TF-IDF(default, descriptive, read-only)/lda(collapsed Gibbs sampling — "
            "alpha/beta priors, iterations)/nmf(multiplicative updates on the TF-IDF matrix) "
            "over 'docs' (pre-tokenized word lists) OR a graph-derived 'source' {node_label, "
            "field, limit} (tokenizes a text property per node — no Tantivy dependency). "
            "Params: k (topic count for lda/nmf), top_n (terms kept per row). writeback ⇒ "
            "(lda/nmf only) :Topic nodes linked HAS_TOPIC from each doc's dominant topic. "
            "Returns {doc_terms:[...]} (tfidf) or {topics:[...], doc_topics:[...]} (lda/nmf). "
            "• subgraph — GRAPH-NATIVE (no input rows — mines the graph itself): "
            "gspan(default — level-wise frequent connected-subgraph pattern growth up to "
            "'max_edges', canonicalized + exactly re-counted; 'min_support' is a fraction of "
            "total host edges; support = raw embedding count, not min-node-image support)/"
            "motif(label-agnostic topological census: wedges/triangles/directed-3-cycles; "
            "min_support/max_edges ignored). Optional 'label' restricts the scanned host "
            "graph to one node type (None = whole resident graph). writeback ⇒ (gspan only) "
            ":FrequentSubgraph nodes linked SUBGRAPH_MEMBER to every node in any embedding. "
            "Returns {patterns:[{nodes,edges,support,count}],...} (gspan) or "
            "{motifs:{wedge,triangle,directed_cycle3},...} (motif). "
            "• entity_resolve — record linkage: which record PAIRS refer to the same "
            "real-world entity, over token 'records' (Jaccard, blocked by 'block_keys') "
            "OR embeddings ('vectors' or a vector 'source' {node_label, field}, Cosine, "
            "blocked by 'bucket_precision'). Params: threshold. writeback ⇒ :EntityMatch "
            "nodes linked to both members. Returns {matches:[{a,b,score}],...}. "
            "• causal_impact — estimate an intervention's causal effect in a time series "
            "at 'intervention_index': interrupted-time-series (post_mean − pre_mean) over "
            "'series' alone, or difference-in-differences when a non-empty 'control' "
            "series is also given (isolates the treatment effect from a shared trend). "
            "Returns {effect_size, standard_error, confidence,...}. writeback (+ "
            "'series_id') ⇒ :CausalEffect node. "
            "• process — process mining over ordered event 'traces' (activity-label "
            "sequences, repeats allowed): mines the directly-follows graph + the "
            "alpha-algorithm footprint (causal 'a>b' / parallel 'a||b' / choice 'a#b') "
            "plus start/end activity sets. Alternatively provide provenance-rich "
            "'events' plus an explicit 'object_type' + 'perspective_id' + "
            "'derivation_version' to deterministically project one NAMED, VERSIONED "
            "object-centric case perspective without an LLM — classical single-case "
            "flattening always requires and discloses this triple; there is no bare "
            "'object_type'-only path (undisclosed flattening is refused). Or provide a "
            "governed 'ocel_json' JSON-OCEL 2.0 document with an authorized 'tenant', "
            "optional source_ref/mapping_version/provenance transport metadata, and "
            "ocel_mode='mine' (default) or 'validate'. 'validate' only validates + exports a "
            "canonical deterministic OCEL document and never writes. 'mine' additionally "
            "requires 'object_type'/'perspective_id'/'derivation_version', commits the OCEL "
            "source truth PLUS the disclosed ProcessPerspective as one tenant-scoped tEKG "
            "ChangeEnvelope (real graph write — not just a plan), and mines traces under that "
            "perspective. Event projection writeback is fail-closed until the native "
            "ProcessModel can retain source-event lineage. "
            "Trace writeback (+ 'process_id') ⇒ :ProcessModel node. "
            "Or provide 'traces'/'object_ids'/'allowed_edges' (a reference model's edge "
            "set, e.g. a discovered directly-follows graph) + 'model_ref' + "
            "'graph_as_of'/'mapping_version'/'export_digest' + the disclosed perspective "
            "triple ('object_type'/'perspective_id'/'derivation_version') + 'tenant' to run "
            "CONFORMANCE CHECKING — 'does THIS run's behavior fit a GIVEN model?', formally "
            "separate from discovery (the model is NEVER re-derived from the traces being "
            "checked). Commits one :ConformanceRun node (linked CHECKED_UNDER_PERSPECTIVE to "
            "the perspective) + one :Deviation node per mismatch (linked HAS_DEVIATION), "
            "querying which is what discovery's own writeback cannot answer. "
            "• root_cause — bounded-depth ('max_hops'), decaying ('decay') backward "
            "search over a weighted dependency graph ('nodes','edges' cause→effect, "
            "per-node anomaly 'scores') to rank the most-likely upstream root cause of a "
            "flagged 'symptom' node. writeback ⇒ :RootCause node linked to the symptom. "
            "• risk_propagation — personalized PageRank over a weighted dependency graph "
            "('nodes','edges'), restarting to a 'seed' risk distribution instead of "
            "teleporting uniformly — propagates risk from seeded nodes along dependency "
            "edges. Params: damping, tolerance, max_iterations. writeback ⇒ one "
            ":RiskScore node per input node. "
            "• ontology_gap — GRAPH-NATIVE completeness scan of the resident graph's own "
            "node-type/edge-relationship class shape (no rdf/OWL-reasoner dependency): a "
            "class with no declared properties, an unresolved subClassOf parent, or a "
            "fully disconnected class. Optional 'label' restricts the scan to one class. "
            "writeback ⇒ :OntologyGap node per gap. "
            "• retrieval_quality — precision@k / recall@k / MRR over stored retrieval "
            "'traces' ([{retrieved,relevant}]) — audits a RAG/search pipeline's own "
            "quality. Params: k. writeback (+ 'query_id') ⇒ :RetrievalQuality node. "
            "• community — wraps the EXISTING Louvain / label-propagation GDS kernels "
            "(adds no new algorithm, only epistemic writeback) over the resident graph, "
            "optional 'label' restriction. Params: algorithm(louvain|label_propagation), "
            "resolution, weighted. writeback ⇒ one :Community node per community, linked "
            "to every member. "
            "REST twins: POST /api/mining/{associate,cluster,anomaly,classify_fit,"
            "classify_predict,reduce,sequence,forecast,text,subgraph,entity_resolve,"
            "causal_impact,process,root_cause,risk_propagation,ontology_gap,"
            "retrieval_quality,community} (same _execute_tool core). Degrades cleanly "
            "on a no-mining engine build."
        ),
        tags=["arbora", "engine", "mining", "clustering", "anomaly", "data-mining"],
    )
    def graph_mine(
        action: str = Field(
            default="associate",
            description="Mining action (18): 'associate' | 'cluster' | 'anomaly' | "
            "'classify_fit' | 'classify_predict' | 'reduce' | 'sequence' | 'forecast' | "
            "'text' | 'subgraph' | 'entity_resolve' (alias 'entity_resolution') | "
            "'causal_impact' | 'process' (alias 'process_mining') | 'root_cause' | "
            "'risk_propagation' | 'ontology_gap' | 'retrieval_quality' | 'community'. "
            "An unrecognized action returns an error listing every valid action.",
        ),
        params_json: str = Field(
            default="{}",
            description="JSON object of mining kwargs, e.g. "
            '{"transactions":[["bread","milk"],["bread","butter"]],'
            '"min_support":0.5,"algorithm":"fpgrowth"} (associate); '
            '{"features":[[0,0],[10,10]],"algorithm":"dbscan","eps":1.0,"min_pts":2} '
            'or {"source":{"node_label":"Doc"},"algorithm":"kmedoids","k":3,'
            '"writeback":true} (cluster); '
            '{"values":[1,1,1,100],"algorithm":"zscore"} or '
            '{"source":{"node_label":"Metric"},"algorithm":"isoforest"} (anomaly); '
            '{"x":[[0,0],[10,10]],"y":[0,1],"algorithm":"logistic"} (classify_fit); '
            '{"model":{...},"x":[[0.1,0.1]]} (classify_predict); '
            '{"x":[[..]],"algorithm":"svd","n_components":2} or '
            '{"source":{"node_label":"Doc"},"algorithm":"umap","writeback":true} (reduce); '
            '{"sequences":[["login","browse","purchase"]],"min_support":0.5} or '
            '{"source":{"node_label":"Session"},"algorithm":"gsp","writeback":true} (sequence); '
            '{"values":[5,8,11,14],"algorithm":"arima","p":1,"d":1,"horizon":5} or '
            '{"values":[...],"algorithm":"holtwinters","period":12,"horizon":12} (forecast); '
            '{"docs":[["the","cat","sat"]],"algorithm":"tfidf"} or '
            '{"source":{"node_label":"Doc","field":"body"},"algorithm":"lda","k":5,'
            '"writeback":true} (text); '
            '{"min_support":0.1,"max_edges":2,"writeback":true} or '
            '{"label":"Concept","algorithm":"motif"} (subgraph); '
            '{"records":[["john","smith"],["jon","smith"]],"block_keys":["smith","smith"],'
            '"threshold":0.5} or {"source":{"node_label":"Person","field":"embedding"},'
            '"threshold":0.85,"writeback":true} (entity_resolve); '
            '{"series":[1,1,1,1,5,5,5,5],"intervention_index":4} or '
            '{"series":[...],"control":[...],"intervention_index":10,"writeback":true} '
            "(causal_impact); "
            '{"traces":[["login","browse","checkout"]]} or '
            '{"traces":[...],"process_id":"checkout-flow","writeback":true} or '
            '{"events":[{"event_id":"e1","activity":"login",'
            '"occurred_at":"2026-01-01T00:00:00Z","source_ref":"src:1",'
            '"objects":[{"id":"u1","type":"User","qualifier":"actor"}]}],'
            '"object_type":"User","perspective_id":"case:user-view",'
            '"derivation_version":"v1"} (process); '
            '{"ocel_json":{"eventTypes":[],"objectTypes":[],"events":[],"objects":[]},'
            '"tenant":"acme",'
            '"object_type":"Order","perspective_id":"case:order-view",'
            '"derivation_version":"v1","ocel_mode":"mine|validate"} (process); '
            '{"traces":[["create","ship"]],"object_ids":["order-1"],'
            '"allowed_edges":[["create","pack"]],"model_ref":"model:v1",'
            '"graph_as_of":"2026-01-01T00:00:00Z","mapping_version":"ocel-json-2.0",'
            '"export_digest":"abc123","object_type":"Order",'
            '"perspective_id":"case:order-view","derivation_version":"v1",'
            '"tenant":"acme"} (process, conformance check); '
            '{"nodes":["a","b"],"scores":[0.1,0.9],"edges":[["a","b",1.0]],"symptom":"b"} '
            "(root_cause); "
            '{"nodes":["a","b"],"seed":[1.0,0.0],"edges":[["a","b",1.0]]} '
            "(risk_propagation); "
            '{"label":"Concept"} or {"writeback":true} (ontology_gap); '
            '{"traces":[{"retrieved":["d1","d2"],"relevant":["d1"]}],"k":2} '
            "(retrieval_quality); "
            '{"label":"Concept","algorithm":"louvain"} or '
            '{"algorithm":"label_propagation","writeback":true} (community).',
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin action-router over the engine mining surface (CONCEPT:MEDULLA-KG.mining.frequent-itemset-mining)."""
        action = (action or "").strip().replace("-", "_") or "associate"
        action = _MINING_ACTION_ALIASES.get(action, action)
        params, rejection = _decode_json_object(params_json, "mining")
        if rejection is not None:
            return rejection
        # CONCEPT:MERISTEM-KG.compute.engine-surface-manifest — an action that isn't one of the
        # 18 real MiningClient methods is a NAME error (typo/guess), not "this engine
        # build lacks mining" — report it as such, with the introspected valid-action
        # list, instead of falling through to _invoke's generic degraded payload (which
        # previously made a guessed name like 'entity_resolution' or 'process_mining'
        # look identical to the whole mining surface being absent).
        valid_actions = _mining_actions()
        if valid_actions and action not in valid_actions:
            return json.dumps(
                {
                    "surface": "mining",
                    "action": action,
                    "error": f"unknown action {action!r} for graph_mine; choose one of "
                    f"{sorted(valid_actions)}",
                    "actions": sorted(valid_actions),
                }
            )
        # The 'process' action's three governed special cases, in their original
        # order: each returns None when its own guard does not match, falling
        # through to the generic _invoke dispatch below.
        for special_case in (_graph_mine_process_events,):
            special_result = special_case(action, params, graph)
            if special_result is not None:
                return special_result
        return _invoke(
            surface="mining",
            action=action,
            graph=graph,
            candidates=(("mining", action),),
            params=params,
        )

    # ══════════════════════════════════════════════════════════════════
    # graph_mine_deep — Phase-6 heavy-dep delegation to data-science-mcp
    # (CONCEPT:MERISTEM-KG.mining.dsm-forecast-delegation)
    # ══════════════════════════════════════════════════════════════════
    @conditional_tool_decorator(
        mcp,
        name="graph_mine_deep",
        excluded_names=frozenset({"graph_mine", "graph_mine_deep"}),
        include_excluded=include_unserved_mining,
        description=(
            "CONCEPT:MERISTEM-KG.mining.dsm-forecast-delegation — the deep-learning / heavy-Python family the "
            "engine core deliberately does NOT implement (no torch/GPU in the "
            "pure-Rust engine): this tool DISPATCHES to agents/data-science-mcp over "
            "MCP (the fleet tool-call helper, same one 'fleet.write_record' "
            "uses) and folds the result back into the KG as typed nodes "
            "(CONCEPT:MERISTEM-KG.mining.foldback-typed-nodes). Actions: 'deep_forecast' (LSTM sequence "
            "forecaster — the delegated Prophet/LSTM family; Prophet itself needs an "
            "unvendored Stan toolchain, ARIMA/Holt-Winters/STL in graph_mine remain "
            "the native classical default), 'deep_classify' (MLP deep classifier), "
            "'autoencoder_anomaly' (reconstruction-error outlier detection), "
            "'xgboost' (histogram gradient-boosting classifier — the documented "
            "xgboost substitute; no separate xgboost package is vendored), 'embed' "
            "(neural embedding of arbitrary NUMERIC feature rows via an autoencoder "
            "bottleneck — NOT text/word embeddings, those stay on the remote vLLM "
            "embedder). Every action accepts either raw rows ('x'/'values') OR a "
            "graph-derived 'source' {node_label, fields(list of properties), limit} "
            "gathered from the KG as a RowSet via one read-only Cypher projection "
            "(compute-near-data — the row-gathering read and the delegated call are "
            "the only two round trips). writeback=true materializes the result as "
            "typed nodes tagged provider='data-science-mcp': ':Forecast' (deep_forecast, "
            "linked FORECAST_OF a 'series_id' node when given), ':Classification' "
            "(deep_classify/xgboost), ':Anomaly' (autoencoder_anomaly), ':Embedding' "
            "(embed) — each row-level node linked DEEP_RESULT_OF its source node when "
            "a 'source' was used. Degrades cleanly (never crashes) when "
            "data-science-mcp is unreachable or its [training] extra (torch) is not "
            "installed: returns {available:false, error:...}. "
            "REST twins: POST /api/mining/deep/{deep_forecast,deep_classify,"
            "autoencoder_anomaly,xgboost,embed} (same _execute_tool core)."
        ),
        tags=[
            "arbora",
            "engine",
            "mining",
            "deep-learning",
            "data-science-mcp",
            "delegation",
        ],
    )
    def graph_mine_deep(
        action: str = Field(
            default="deep_forecast",
            description="Delegated mining action: 'deep_forecast' | 'deep_classify' "
            "| 'autoencoder_anomaly' | 'xgboost' | 'embed'.",
        ),
        params_json: str = Field(
            default="{}",
            description="JSON object of kwargs, e.g. "
            '{"values":[5,8,11,14,18],"horizon":5,"lookback":3,"series_id":"metric:cpu",'
            '"writeback":true} (deep_forecast); '
            '{"x":[[0,0],[10,10]],"y":[0,1],"epochs":100,"writeback":true} or '
            '{"source":{"node_label":"Doc","fields":["f1","f2"],"limit":200},"y":[0,1,...]} '
            "(deep_classify/xgboost); "
            '{"x":[[0,0],[0,1],[50,50]],"bottleneck":2,"writeback":true} or '
            '{"source":{"node_label":"Metric","fields":["v"],"limit":500}} '
            "(autoencoder_anomaly/embed).",
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin delegation adapter: ship features to data-science-mcp, fold predictions back (CONCEPT:MERISTEM-KG.mining.dsm-forecast-delegation)."""
        action, rejection = _deep_action_or_error(action)
        if rejection is not None:
            return rejection
        params, rejection = _decode_json_object(
            params_json, "mining_deep", action=action
        )
        if rejection is not None:
            return rejection

        writeback = bool(params.pop("writeback", False))
        series_id = str(params.pop("series_id", "") or "")

        try:
            tool_params, node_ids = _prepare_deep_delegation(
                tools, action, params, graph
            )
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "bad input / feature-gathering failure is data", exc_info=True
            )
            return _surface_error(exc, surface="mining_deep", action=action)

        raw, delegate_error = _deep_call_delegate(action, tool_params)
        if delegate_error is not None:
            return delegate_error

        result = raw.get("result") or {}
        written = (
            _deep_writeback(tools, action, result, node_ids, series_id, graph)
            if writeback
            else []
        )
        return json.dumps(
            {
                "surface": "mining_deep",
                "action": action,
                "provider": _DSM_SERVER_NAME,
                "delegated": True,
                "available": True,
                "result": result,
                "written_node_ids": written,
            },
            default=_json_default,
        )

    register_conditional_tools(
        tools,
        [
            ("graph_mine", graph_mine, "/mining/associate"),
            (
                "graph_mine_deep",
                graph_mine_deep,
                "/mining/deep/deep_forecast",
            ),
        ],
        excluded_names=frozenset({"graph_mine", "graph_mine_deep"}),
        include_excluded=include_unserved_mining,
        route_registry=runtime.ACTION_TOOL_ROUTES,
    )
