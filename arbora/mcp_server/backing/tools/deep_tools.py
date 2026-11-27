"""The broker, federated search, gis, memory, learn, pipeline and fork engine-surface tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.domain.mcp_server.backing.context_helpers import run_coro as _run_coro
from arbora.api.errors import not_served
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.tools.engine_surface_common import (
    _decode_json_object,
    _drop_empty,
    _invoke,
    _json_default,
    _surface_error,
)
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


# Candidate ``(sub_client_attr, method_attr)`` probe lists per logical action. The
# engine build / client may expose the surface under any of several plausible
# namespaces; the first callable found wins and everything else degrades cleanly.
_FEDERATED_SEARCH_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("search", "federated_search"),
    ("query", "federated_search"),
    ("federation", "search"),
    ("query", "federated"),
)


# graph_memory — MEDULLA-318 memory / scene / trajectory ops. Each logical action maps
# to a small probe list of ``(sub_client_attr, method_attr)`` paths the engine
# build / client may plausibly expose the MEDULLA-318 wire Method under (the client
# wraps each wire ``Method`` as a snake_case method on a sub-client). The first
# callable found wins; when none resolve the tool degrades cleanly. Any action not
# listed here falls back to probing the three sub-clients with the action name
# directly, so read ops (get_summary / get_scene / get_trajectory / …) light up
# by name once the engine ships them.
_MEMORY_ACTION_CANDIDATES: Mapping[str, tuple[tuple[str, str], ...]] = MappingProxyType(
    {
        # episodic → semantic memory (CreateSummaryNode / Consolidate / Maintain)
        "create_summary": (
            ("memory", "create_summary"),
            ("memory", "create_summary_node"),
        ),
        "consolidate": (("memory", "consolidate"),),
        "maintain": (("memory", "maintain"),),
        # spatial scene graph (AddSceneObject / world transform)
        "add_scene_object": (
            ("scene", "add_object"),
            ("scene", "add_scene_object"),
            ("memory", "add_scene_object"),
        ),
        "world_transform": (
            ("scene", "world_transform"),
            ("scene", "set_world_transform"),
        ),
        # trajectories / RL episodes (StartTrajectory / AppendStep / discounted return)
        "start_trajectory": (
            ("trajectory", "start"),
            ("trajectory", "start_trajectory"),
        ),
        "append_step": (
            ("trajectory", "append_step"),
            ("trajectory", "append"),
        ),
        "discounted_return": (("trajectory", "discounted_return"),),
    }
)


# CONCEPT:MERISTEM-KG.memory.unified-memory-crud-core — the unified memory-CRUD core. graph_memory's recall/store/link
# actions route into the SAME ``graph_write`` tool the REST ``/graph/write/memory``
# [``/recall``] twins and the harness ``kg_memory_recall``/``kg_memory_store`` tools
# already use — one core, no fourth memory surface. ``link`` reuses graph_write's
# ``add_edge`` so relating two memories rides the same mutation path.
_MEMORY_CRUD_ACTIONS = ("recall", "store", "link")


def _memory_crud(tools: ToolTable, action: str, params: dict[str, Any]) -> str:
    """Dispatch recall/store/link into the shared ``graph_write`` memory core."""
    if action == "store":
        call = dict(
            action="store_memory",
            agent_id=params.get("agent_id", params.get("agent", "")),
            node_type=params.get("memory_type", params.get("type", "")),
            properties=params.get("content", params.get("properties", "")),
            nodes=json.dumps(params.get("tags", [])),
        )
    elif action == "recall":
        call = dict(
            action="recall_memory",
            properties=params.get("query", params.get("content", "")),
            node_type=params.get("memory_type", params.get("type", "")),
        )
    else:  # link
        src = params.get("source", params.get("source_id", ""))
        tgt = params.get("target", params.get("target_id", ""))
        if not src or not tgt:
            return json.dumps(
                {
                    "surface": "memory",
                    "action": "link",
                    "error": "link requires 'source' and 'target' (memory node ids)",
                }
            )
        call = dict(
            action="add_edge",
            source_id=src,
            target_id=tgt,
            rel_type=params.get("rel_type", "RELATES_TO"),
            properties=json.dumps(params.get("properties", {})),
        )
    try:
        result = _run_coro(runtime._execute_tool(tools, "graph_write", **call))
    except Exception as exc:
        logging.getLogger(__name__).debug(
            "surface engine/core errors as data", exc_info=True
        )
        return _surface_error(exc, surface="memory", action=action)
    return json.dumps(
        {"surface": "memory", "action": action, "result": result}, default=_json_default
    )


def _warm_forkable_sandboxes(preferred: str) -> list[Any]:
    """The registry's warm-fork-capable rungs, cheapest first.

    Reuses the ORCH-1.86 sandbox registry (``default_sandboxes()``); ``preferred``
    pins a rung by name when set (and is ignored when it matches nothing). An
    unimportable subsystem degrades to an empty list.
    """
    return []


def _sandbox_available(sb: Any) -> bool:
    """Whether a rung probes as available; an unprobeable rung is simply skipped."""
    try:
        return bool(sb.is_available())
    except Exception:
        logging.getLogger(__name__).debug(
            "an unprobeable rung is simply skipped", exc_info=True
        )
        return False


def _pick_warm_fork_sandbox(preferred: str = "") -> Any:
    """Return the cheapest available warm-fork rung, or ``None`` (CONCEPT:MERISTEM-KG.coordination.warm-fork-fanout).

    Reuses the ORCH-1.86 sandbox registry (``default_sandboxes()``, cheapest-first)
    and selects the first backend whose capabilities advertise ``warm_fork`` and
    which is available on this host. ``preferred`` pins a rung by name when set.
    """
    for sb in _warm_forkable_sandboxes(preferred):
        if _sandbox_available(sb):
            return sb
    return None


def _fork_fanout(branches: list[Any], seed_vars: dict[str, Any], preferred: str) -> str:
    """Warm a fork parent once and fan out ``branches``, returning per-branch results.

    Backed by the ORCH-1.86..93 warm-fork primitive: the base
    :class:`ForkableSandbox.execute` warms-or-reuses one parent (copy-on-write) and
    forks a child per branch. Fan-out is concurrent ``execute`` calls sharing that
    one warm parent. Degrades cleanly to a structured ``unavailable`` payload when no
    warm-fork rung is available on this host (CONCEPT:MERISTEM-KG.coordination.warm-fork-fanout).
    """
    sb = _pick_warm_fork_sandbox(preferred)
    if sb is None:
        return json.dumps(
            {
                "surface": "fork",
                "degraded": True,
                "error": (
                    "no warm-fork rung available on this host (firecracker needs a "
                    "reachable governed forkd controller + KVM), and the "
                    "medulla engine client exposes no warm-fork primitive"
                ),
                "followup": (
                    "spike: surface a first-class engine warm-fork/KV-cache-fork op "
                    "on the medulla client (LMCacheMPConnector snapshot → "
                    "branch), then route graph_fork to that governed primitive"
                ),
                "branch_count": len(branches),
            }
        )

    raise not_served(
        "rlm.sandboxes.base",
        "Meristem-harness (deleted from AU)",
        owner="meristem",
    )


def _fork_branches(branches_json: str, code: str, n: int) -> tuple[Any, str | None]:
    """Resolve ``graph_fork``'s branch list from ``branches_json``, else ``code`` + ``n``."""
    try:
        branches = json.loads(branches_json) if branches_json else []
    except (TypeError, ValueError) as exc:
        return None, _surface_error(exc, surface="fork", code="invalid_request")
    if not isinstance(branches, list):
        return None, json.dumps(
            {"surface": "fork", "error": "branches_json must decode to a list"}
        )
    if branches:
        return branches, None
    if code and int(n) > 0:
        return [code] * int(n), None
    return None, json.dumps(
        {
            "surface": "fork",
            "error": "provide branches_json (list) or code + n (>0)",
        }
    )


def register_deep_tools(mcp, tools: ToolTable) -> None:
    """Register the deep_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # graph_broker — engine message broker (exchanges / queues / streams)
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_broker",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — the medulla engine message broker "
            "(AMQP-style exchanges + queues + streams). Action-routed 1:1 over the engine broker "
            "surface: set 'action' to the broker method — e.g. 'declare_exchange' "
            "(exchange [+exchange_type]), 'declare_queue' (queue), 'bind' (queue + "
            "exchange [+routing_key]), 'publish' (exchange + routing_key + payload), "
            "'consume' (queue [+max_messages,+ack]), 'stats' / 'list_queues' / "
            "'list_exchanges'. Extra kwargs go via params_json. Degrades cleanly when "
            "the engine build has no broker surface."
        ),
        tags=["arbora", "engine", "broker", "messaging"],
    )
    def graph_broker(
        action: str = Field(
            default="stats",
            description="Broker method: declare_exchange | declare_queue | bind | "
            "publish | consume | stats | list_queues | list_exchanges | ...",
        ),
        exchange: str = Field(default="", description="Exchange name."),
        queue: str = Field(default="", description="Queue name."),
        routing_key: str = Field(default="", description="Routing/binding key."),
        payload: str = Field(default="", description="Message body (publish)."),
        exchange_type: str = Field(
            default="", description="Exchange type: direct | fanout | topic (declare)."
        ),
        params_json: str = Field(
            default="{}",
            description='JSON object of extra kwargs, e.g. {"max_messages":10,'
            '"ack":true,"durable":true}. Merged over the typed fields.',
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine broker surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        if not action:
            return json.dumps({"surface": "broker", "error": "action is required"})
        try:
            extra = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="broker", code="invalid_request")
        if not isinstance(extra, dict):
            return json.dumps(
                {"surface": "broker", "error": "params_json must decode to an object"}
            )
        params = _drop_empty(
            exchange=exchange,
            queue=queue,
            routing_key=routing_key,
            payload=payload,
            exchange_type=exchange_type,
        )
        params.update(extra)
        return _invoke(
            surface="broker",
            action=action,
            graph=graph,
            candidates=(("broker", action),),
            params=params,
        )

    tools["graph_broker"] = graph_broker

    tools.bind_route("graph_broker", "/graph/broker")

    # ══════════════════════════════════════════════════════════════════
    # graph_federated_search — search across registered external graphs
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_federated_search",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — federated search fanned across registered external "
            "graph references. Provide a natural-language / keyword 'query'; optionally "
            "scope to specific 'references' (comma-separated ExternalGraphReference ids) "
            "and cap with 'top_k'. Extra engine kwargs via params_json. Degrades cleanly "
            "when the engine build has no federated-search surface."
        ),
        tags=["arbora", "engine", "search", "federated"],
    )
    def graph_federated_search(
        query: str = Field(description="Search query (natural language or keywords)."),
        references: str = Field(
            default="",
            description="Comma-separated external graph reference ids (empty ⇒ all).",
        ),
        top_k: int = Field(default=10, description="Max results to return."),
        params_json: str = Field(
            default="{}", description="JSON object of extra engine kwargs."
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine federated-search surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        try:
            extra = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(
                exc, surface="federated_search", code="invalid_request"
            )
        if not isinstance(extra, dict):
            return json.dumps(
                {
                    "surface": "federated_search",
                    "error": "params_json must decode to an object",
                }
            )
        refs = [r.strip() for r in references.split(",") if r.strip()]
        params: dict[str, Any] = {"query": query, "top_k": int(top_k)}
        if refs:
            params["references"] = refs
        params.update(extra)
        return _invoke(
            surface="federated_search",
            action="search",
            graph=graph,
            candidates=_FEDERATED_SEARCH_CANDIDATES,
            params=params,
        )

    tools["graph_federated_search"] = graph_federated_search

    tools.bind_route("graph_federated_search", "/graph/federated-search")

    # ══════════════════════════════════════════════════════════════════
    # graph_gis — geospatial route / tile / geo-task ops
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_gis",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — the engine's GIS surface. Action-routed 1:1 over the "
            "engine geo methods: e.g. 'route' (from + to [+profile]), 'tile' (z/x/y), "
            "'nearest' (lat + lon [+limit]), 'geo_task' (a named geospatial job). All "
            'structured args go via params_json (e.g. {"from":[lat,lon],"to":[lat,'
            "lon]}). Degrades cleanly when the engine build has no GIS surface."
        ),
        tags=["arbora", "engine", "gis", "geospatial"],
    )
    def graph_gis(
        action: str = Field(
            default="route",
            description="GIS method: route | tile | nearest | geo_task | ...",
        ),
        params_json: str = Field(
            default="{}",
            description="JSON object of kwargs for the GIS method (coordinates, "
            "profile, tile z/x/y, task name, ...).",
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine GIS surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        if not action:
            return json.dumps({"surface": "gis", "error": "action is required"})
        try:
            params = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="gis", code="invalid_request")
        if not isinstance(params, dict):
            return json.dumps(
                {"surface": "gis", "error": "params_json must decode to an object"}
            )
        return _invoke(
            surface="gis",
            action=action,
            graph=graph,
            candidates=(("gis", action), ("geo", action)),
            params=params,
        )

    tools["graph_gis"] = graph_gis

    tools.bind_route("graph_gis", "/graph/gis")

    # ══════════════════════════════════════════════════════════════════
    # graph_memory — MEDULLA-318 memory / scene / trajectory engine ops
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_memory",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — the engine's MEDULLA-318 memory surface: episodic→semantic "
            "memory, the spatial scene graph, and RL trajectories. Action-routed 1:1 "
            "over the engine memory methods (dashes normalize to underscores): "
            "'create_summary' (episodic nodes → a summary node — node_ids [+window]), "
            "'consolidate' (roll episodic into semantic memory), 'maintain' (decay / "
            "prune / re-index the memory store), 'add_scene_object' (object_id [+pose/"
            "transform/parent]), 'world_transform' (object_id + transform → world pose), "
            "'start_trajectory' (agent/episode → trajectory_id), 'append_step' "
            "(trajectory_id + step {state,action,reward,...}), 'discounted_return' "
            "(trajectory_id [+gamma]). Read ops (e.g. 'get_summary', 'get_scene', "
            "'get_trajectory') route by action name too. UNIFIED memory-CRUD "
            "(CONCEPT:MERISTEM-KG.memory.unified-memory-crud-core) — 'store' (agent_id + content [+memory_type,+tags]), "
            "'recall' (query [+memory_type]), 'link' (source + target [+rel_type]) — "
            "route into the SAME graph_write memory core as the REST "
            "/graph/write/memory[/recall] twins and the harness kg_memory_recall/store "
            "tools (one core, no separate surface). Structured args go via params_json. "
            "Degrades cleanly when the engine build has no memory surface."
        ),
        tags=["arbora", "engine", "memory", "scene", "trajectory"],
    )
    def graph_memory(
        action: str = Field(
            default="consolidate",
            description="Memory method: create_summary | consolidate | maintain | "
            "add_scene_object | world_transform | start_trajectory | append_step | "
            "discounted_return | get_* | ...",
        ),
        params_json: str = Field(
            default="{}",
            description="JSON object of kwargs for the memory method, e.g. "
            '{"node_ids":["n1","n2"]}, {"object_id":"o1","transform":[...]}, '
            '{"trajectory_id":"t1","step":{"state":...,"action":...,"reward":1.0}}, '
            'or {"trajectory_id":"t1","gamma":0.99}.',
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin wrapper over the engine MEDULLA-318 memory surface (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        action = (action or "").strip().replace("-", "_")
        if not action:
            return json.dumps({"surface": "memory", "error": "action is required"})
        try:
            params = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="memory", code="invalid_request")
        if not isinstance(params, dict):
            return json.dumps(
                {"surface": "memory", "error": "params_json must decode to an object"}
            )
        # CONCEPT:MERISTEM-KG.memory.unified-memory-crud-core — unified memory-CRUD short-circuit: recall/store/link go
        # to the shared graph_write memory core (same as REST + harness), not the
        # engine MEDULLA-318 surface.
        if action in _MEMORY_CRUD_ACTIONS:
            return _memory_crud(tools, action, params)
        candidates = _MEMORY_ACTION_CANDIDATES.get(
            action,
            (("memory", action), ("scene", action), ("trajectory", action)),
        )
        return _invoke(
            surface="memory",
            action=action,
            graph=graph,
            candidates=candidates,
            params=params,
        )

    tools["graph_memory"] = graph_memory

    tools.bind_route("graph_memory", "/graph/memory")

    # ══════════════════════════════════════════════════════════════════
    # graph_learn — graph-learning / neuro-symbolic surface (CONCEPT:MEDULLA-KG.graphlearn.link-predictor)
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_learn",
        description=(
            "CONCEPT:MEDULLA-KG.graphlearn.link-predictor — a pure-Rust KAN (Kolmogorov-"
            "Arnold) link-predictor over the resident graph, whose learned per-feature "
            "edge functions are THEMSELVES queryable KG nodes (interpretability, not raw "
            "accuracy). Actions: 'fit' (learn a model) and 'predict' (score links). "
            "• fit — learn over a graph-derived subgraph: every node with 'node_label' is "
            "a vertex; edges among them (direction out|in|any, optional 'relation') are "
            "positives, non-edges are sampled negatives. Params: basis(chebyshev|jacobi), "
            "degree, hidden(0 ⇒ one interpretable edge fn per feature), epochs, lr, "
            "neg_ratio, seed, alpha, limit, writeback. writeback ⇒ :EdgeFunction nodes "
            "(the learned per-feature curves — Cypher-queryable). Returns {model, n_nodes, "
            "n_edges, train_auc, edge_functions:[{feature,coefficients}], ...}. Pass the "
            "returned 'model' to predict. "
            "• predict — score candidate links with a fitted 'model' over the same "
            "'node_label' subgraph: explicit 'candidate_pairs' [[src,dst],...] OR the "
            "'top_k' highest-probability MISSING links. writeback ⇒ :PredictedEdge nodes "
            "linked to their endpoints. Returns {predicted:[{src,dst,score}], n_predicted, "
            "model, ...}. "
            "Structural features: common-neighbors, Jaccard, Adamic-Adar, preferential "
            "attachment, PageRank-product, neighbor-cosine, 1-hop-aggregated node-feature "
            "dot. Heavy multi-layer KAN-GNN training stays a data-science-mcp/torch job. "
            "REST twins: POST /api/graphlearn/{fit,predict} (same _execute_tool core). "
            "Degrades cleanly on a no-graphlearn engine build."
        ),
        tags=[
            "arbora",
            "engine",
            "graphlearn",
            "link-prediction",
            "kan",
            "neuro-symbolic",
        ],
    )
    def graph_learn(
        action: str = Field(
            default="fit",
            description="Graph-learning action: 'fit' | 'predict'.",
        ),
        params_json: str = Field(
            default="{}",
            description="JSON object of graph-learning kwargs, e.g. "
            '{"node_label":"Person","direction":"any","degree":4,"epochs":200,'
            '"writeback":true} (fit); '
            '{"model":{...},"node_label":"Person","top_k":20,"writeback":true} '
            'or {"model":{...},"node_label":"Person",'
            '"candidate_pairs":[["a","b"],["c","d"]]} (predict).',
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin action-router over the engine graph-learning surface (CONCEPT:MEDULLA-KG.graphlearn.link-predictor)."""
        action = (action or "").strip().replace("-", "_") or "fit"
        try:
            params = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="graphlearn", code="invalid_request")
        if not isinstance(params, dict):
            return json.dumps(
                {
                    "surface": "graphlearn",
                    "error": "params_json must decode to an object",
                }
            )
        return _invoke(
            surface="graphlearn",
            action=action,
            graph=graph,
            candidates=((("graphlearn", action),)),
            params=params,
        )

    tools["graph_learn"] = graph_learn

    # REST twin path: POST {prefix}/graphlearn/fit (bespoke natural-body mount in
    # runtime → the SAME _execute_tool core as the MCP verb).
    tools.bind_route("graph_learn", "/graphlearn/fit")

    # ══════════════════════════════════════════════════════════════════
    # graph_pipeline — composable ML pipeline surface (CONCEPT:MEDULLA-KG.mining.ml-pipeline)
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_pipeline",
        description=(
            "CONCEPT:MEDULLA-KG.mining.ml-pipeline — a composable train→eval→serve→predict "
            "ML pipeline over a VERSIONED ':Model' artifact that GENERALIZES the KAN "
            "one-off: a spec of 'feature steps → split → a pluggable model family'. "
            "Families: 'classify' (node classification — gaussiannb/multinomialnb/knn/"
            "logistic/svc), 'estimator' (regression — ridge/lasso/elasticnet/"
            "decisiontree/randomforest/gradientboosting/adaboost/svr), 'graphlearn' "
            "(the KAN link-predictor). Actions: "
            "• train — fit over composed features, evaluate on a held-out split, and "
            "persist a versioned :Model node. params: {name, spec:{features:[{step:"
            "'embedding',method:'fastrp'|'node2vec',dim,...} | {step:'node_vector'} | "
            "{step:'normalize'}], split:{test_ratio,shuffle,seed}, label_property, "
            "model:{family,algorithm,params}}, source:{node_label,direction,relation,"
            "limit} OR x:[[...]], y:[...], writeback}. Returns {name, version, model_id, "
            "family, metrics:{train,test}, ...}. Re-running train bumps the version. "
            "• eval — score a stored model (version; 0⇒served) against a labeled set → "
            "{metrics}. "
            "• serve — deploy version N as the served :ServedModel (predict-by-name "
            "then resolves it). "
            "• predict — apply a stored model to a source/x; writeback ⇒ :Prediction "
            "nodes. Returns {rows:[{id,label|value,proba}], ...}. "
            "• compare — diff two versions' held-out metrics → {metrics_a, metrics_b, "
            "diff}. "
            "Node classification: 'source' selects the subgraph, an 'embedding' feature "
            "step builds structural node vectors (fastrp/node2vec), 'label_property' is "
            "the per-node integer class. Heavy/deep training stays a data-science-mcp "
            "job. REST twins: POST /api/pipeline/{train,eval,serve,predict,compare}. "
            "Degrades cleanly on a no-ml-pipeline engine build."
        ),
        tags=[
            "arbora",
            "engine",
            "ml-pipeline",
            "pipeline",
            "node-classification",
            "model-registry",
        ],
    )
    def graph_pipeline(
        action: str = Field(
            default="train",
            description="Pipeline action: 'train' | 'eval' | 'serve' | 'predict' | 'compare'.",
        ),
        params_json: str = Field(
            default="{}",
            description="JSON object of pipeline kwargs, e.g. "
            '{"name":"community","spec":{"features":[{"step":"embedding",'
            '"method":"fastrp","dim":32}],"split":{"test_ratio":0.3},'
            '"label_property":"label","model":{"family":"classify",'
            '"algorithm":"logistic"}},"source":{"node_label":"Person"}} (train); '
            '{"name":"community","version":1} (serve); '
            '{"name":"community","version":0,"source":{"node_label":"Person"},'
            '"writeback":true} (predict); '
            '{"name":"community","version_a":1,"version_b":2} (compare).',
        ),
        graph: str = Field(
            default="", description="Target graph (empty ⇒ deployment default)."
        ),
    ) -> str:
        """Thin action-router over the engine ML-pipeline surface (CONCEPT:MEDULLA-KG.mining.ml-pipeline)."""
        # MCP verb → client method (the 'eval' alias maps to the client's evaluate()).
        method_by_action = {
            "train": "train",
            "eval": "evaluate",
            "evaluate": "evaluate",
            "serve": "serve",
            "predict": "predict",
            "compare": "compare",
        }
        action = (action or "").strip().replace("-", "_").lower() or "train"
        method = method_by_action.get(action)
        if method is None:
            return json.dumps(
                {
                    "surface": "pipeline",
                    "error": f"unknown action {action!r} for graph_pipeline; choose one of "
                    "train | eval | serve | predict | compare",
                }
            )
        try:
            params = json.loads(params_json) if params_json else {}
        except (TypeError, ValueError) as exc:
            return _surface_error(exc, surface="pipeline", code="invalid_request")
        if not isinstance(params, dict):
            return json.dumps(
                {"surface": "pipeline", "error": "params_json must decode to an object"}
            )
        return _invoke(
            surface="pipeline",
            action=method,
            graph=graph,
            candidates=((("pipeline", method),)),
            params=params,
        )

    tools["graph_pipeline"] = graph_pipeline

    # REST twin path: POST {prefix}/pipeline/train (natural-body mount in runtime →
    # the SAME _execute_tool core as the MCP verb).
    tools.bind_route("graph_pipeline", "/pipeline/train")

    # ══════════════════════════════════════════════════════════════════
    # graph_fork — warm-fork / KV-cache fan-out (CONCEPT:MERISTEM-KG.coordination.warm-fork-fanout)
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_fork",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.warm-fork-fanout — warm-fork fan-out over the ORCH-1.86..93 warm-fork "
            "primitive (LMCache KV / copy-on-write sandboxes): pay warm-up ONCE for a "
            "parent context, then fork N copy-on-write branches to run per-branch "
            "computations concurrently and return each branch's result. Provide either "
            "'branches_json' (a JSON list of per-branch code snippets) or 'code' + 'n' "
            "(run the same snippet across n branches); 'vars_json' seeds the shared "
            "namespace forked into every branch; 'sandbox' optionally pins the "
            "firecracker rung, else the cheapest available "
            "warm-fork rung is used. Degrades cleanly "
            "(structured 'unavailable') when no warm-fork rung is available on this host."
        ),
        tags=["arbora", "engine", "fork", "warm-fork", "fanout"],
    )
    def graph_fork(
        code: str = Field(
            default="",
            description="A single code snippet run on each of 'n' branches (ignored "
            "when 'branches_json' is provided).",
        ),
        n: int = Field(
            default=0, description="Fan-out count when using 'code' (branches to fork)."
        ),
        branches_json: str = Field(
            default="[]",
            description="JSON list of per-branch code snippets; overrides code/n.",
        ),
        vars_json: str = Field(
            default="{}",
            description="JSON object seeding the namespace forked into every branch.",
        ),
        sandbox: str = Field(
            default="",
            description="Preferred warm-fork rung name (empty ⇒ cheapest available).",
        ),
    ) -> str:
        """Thin verb over the warm-fork primitive (CONCEPT:MERISTEM-KG.coordination.warm-fork-fanout)."""
        branches, rejection = _fork_branches(branches_json, code, n)
        if rejection is not None:
            return rejection
        seed_vars, rejection = _decode_json_object(vars_json, "fork", field="vars_json")
        if rejection is not None:
            return rejection
        return _fork_fanout(branches, seed_vars, sandbox.strip())

    tools["graph_fork"] = graph_fork

    tools.bind_route("graph_fork", "/graph/fork")
