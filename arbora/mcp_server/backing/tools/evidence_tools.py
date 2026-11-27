"""The search, synthesis and document-tree evidence tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from pydantic import Field

from arbora.api.domain.mcp_server.backing import engine_routing
from arbora.api.errors import not_served
from arbora.core.event_loop import run_blocking_ordered
from arbora.mcp_server.host_caches import ToolTable
from arbora.security.error_surface import public_error_text

logger = logging.getLogger(__name__)


_GRAPH_SEARCH_RESULTS_MODES = frozenset(
    {
        "hyde",
        "deep",
        "hybrid",
        "concept",
        "analogy",
        "adore",
        "chrono_ids",
        "dci",
        "memory",
        "latent",
        "sira",
        "rerank",
    }
)


def _graph_search_reorder(engine: Any, session: Any, *, query: str, top_k: int) -> Any:
    # Semantic-retrieval hybrid re-scoring of a candidate set.
    raise not_served(
        "knowledge_graph.retrieval.semantic_retrieval_engine",
        "MEDULLA-FEDERATED-QUERY-R652",
    )


def _graph_search_produce_results_extended(
    engine: Any, session: Any, *, mode: str, query: str, top_k: int
) -> Any:
    if mode == "adore":
        return engine.search_adore(query=query, top_k=top_k)
    if mode == "chrono_ids":
        return engine.temporal_semantic_ids(query=query, top_k=top_k)
    if mode == "dci":
        # search_dci is fail-closed (CONCEPT:MERISTEM-KG.retrieval.acl-aware-vector-retrieval):
        # it always resolves a session and raises rather than returning an
        # unfiltered traversal, so this served path must pass the already-ambient
        # verified session (same pattern the hybrid/hyde/deep/concept/analogy
        # branches use) rather than relying on an implicit fallback.
        return engine.search_dci(query=query, top_k=top_k, session=session)
    if mode == "memory":
        return engine.search_memories(query=query, top_k=top_k)
    if mode == "latent":
        # KG-2.3 — route through the latent topology hierarchy.
        raise not_served(
            "knowledge_graph.retrieval.latent_topology_rag",
            "MEDULLA-FEDERATED-QUERY-R652",
        )
    if mode == "sira":
        # Single-shot SIRA: hybrid-retrieve, then sparsity-align the set.
        raise not_served(
            "knowledge_graph.retrieval.single_shot_sira", "MEDULLA-FEDERATED-QUERY-R652"
        )
    # Only "rerank" remains among `_GRAPH_SEARCH_RESULTS_MODES` at this point —
    # the caller has already dispatched hyde/deep/hybrid/concept/analogy above,
    # and every other results-producing mode just above.
    return _graph_search_reorder(engine, session, query=query, top_k=top_k)


def _graph_search_produce_results(
    engine: Any,
    session: Any,
    *,
    mode: str,
    query: str,
    top_k: int,
    self_correct: bool,
    as_of: str,
) -> Any:
    """Modes in `_GRAPH_SEARCH_RESULTS_MODES` that feed the shared formatter
    in `_search_with_engine` below. Precondition: `mode` is a member."""
    if mode in ("hyde", "deep"):
        return engine.search_hybrid(
            query=query,
            top_k=top_k,
            mode=mode,
            self_correct=self_correct,
            session=session,
        )
    if mode == "hybrid":
        return engine.search_hybrid(
            query=query,
            top_k=top_k,
            self_correct=self_correct,
            as_of=as_of or None,
            session=session,
        )
    if mode in ("concept", "analogy"):
        return engine.search_hybrid(query=query, top_k=top_k, session=session)
    return _graph_search_produce_results_extended(
        engine, session, mode=mode, query=query, top_k=top_k
    )


def _graph_search_hard_negatives(engine: Any, query: str) -> str:
    # KG-2.3 — mine hard negatives via the engine's hybrid retriever.
    raise not_served(
        "knowledge_graph.retrieval.hard_negative_miner", "MEDULLA-FEDERATED-QUERY-R008"
    )


def _graph_search_compiled(
    engine: Any, query: str, *, top_k: int, as_of: str, token_budget: int
) -> str:
    # CONCEPT:MERISTEM-KG.retrieval.context-compiler — policy-aware ContextCompiler bundle: reuses the
    # SAME engine ANN/hybrid retriever the other modes call, but additionally
    # MMR-diversifies, scores evidence-quality/freshness from the epistemic
    # columns (EPI-P3-1), fits a token budget, and runs every candidate through
    # the live permissioning gate before returning citations + a proof graph —
    # replacing the plain relevance-sorted concat the shared formatter performs
    # for every other mode.
    raise not_served(
        "knowledge_graph.retrieval.context_compiler", "MEDULLA-FEDERATED-QUERY-R650"
    )


def _graph_search_format_results(results: Any) -> str:
    # The direct retrieval modes use a flat, score-sorted text block with no
    # diversity/evidence/freshness/policy/budget shaping — prefer
    # mode='compiled' for a citation- and proof-graph-bearing bundle.
    formatted_results = []
    for res in results:
        score = res.get("score", 0)
        score = float(score) if score is not None else 0.0
        node = res.get("node", res)
        label = node.get("type", node.get("label", "Unknown"))
        name = node.get("name", "Unnamed")
        desc = node.get("description", "")
        nid = node.get("id", "N/A")
        formatted_results.append(
            f"[{label}] {name} (ID: {nid}) - Score: {score:.2f}\n{desc}"
        )
    return "\n---\n".join(formatted_results)


def _graph_search_single_target(
    entries: list[tuple[str, Any]],
    graph: str,
    run_search: Callable[[Any], str],
) -> str:
    """Single-connection ``graph_search`` path: bind the graph and run once."""
    name, engine = entries[0]
    try:
        with engine_routing.bound_to_graph(graph):
            text = run_search(engine)
    except PermissionError as e:
        return public_error_text(
            e, code="permission_denied" if graph else "operation_failed"
        )
    return f"{text}\n\n[connection={name} graph={graph or '(default)'}]"


def _graph_search_implicit_fanout(
    entries: list[tuple[str, Any]],
    run_search: Callable[[Any], str],
    content_graph_search: Callable[[str, Any], str],
) -> tuple[dict[str, Any], dict[str, str]]:
    """CONCEPT:MERISTEM-KG.ingest.unified-query-routing — an implicit-default connection
    (no ``connection`` passed, or explicitly "default") fans across every active
    content graph, which can be dozens of ``code:<repo>``/``src:<repo>``
    connections, often idle/unreachable.

    The PRIMARY/``default`` backend must ALWAYS ground: it holds the primary
    ``__commons__`` + control-plane content and is the source of the real
    ranked hits. So run it separately at the normal budget (never the
    skip-timeout) and apply the SHORT skip-timeout ONLY to the supplementary
    content backends. Under a wide implicit fan-out (~70 ``code:*`` graphs) a
    single shared short wall-clock across ALL targets starved the primary —
    it was queued behind the hung code backends and timed out too, so the
    search returned zero results even though the engine was healthy.
    Splitting the primary out fixes that: a search still returns the
    primary's real hits in a few seconds when every supplementary backend is
    dead.
    """
    primary = [(n, e) for n, e in entries if n == "default"]
    supplementary = [(n, e) for n, e in entries if n != "default"]
    # Robustness: if the resolver produced no ``default`` entry, treat the
    # first as primary so SOMETHING always grounds at the full budget.
    if not primary and entries:
        primary, supplementary = [entries[0]], entries[1:]
    results: dict[str, Any] = {}
    fan_errors: dict[str, str] = {}
    for name, engine in primary:
        # B-18: bind for a real content graph promoted into `primary` by
        # the robustness fallback above too — only the literal-"default"
        # entry already targets the ambient session's own graph and needs
        # no narrowing.
        if name == "default":
            results[name] = run_search(engine)
        else:
            results[name] = content_graph_search(name, engine)
    if supplementary:
        sup_results, sup_errors = engine_routing.fanout_execute(
            supplementary,
            content_graph_search,
            timeout=engine_routing.DEFAULT_CONTENT_FANOUT_TIMEOUT_S,
        )
        results.update(sup_results)
        fan_errors.update(sup_errors)
    return results, fan_errors


def _graph_search_fanout(
    entries: list[tuple[str, Any]],
    errors: dict[str, str],
    connection: str,
    run_search: Callable[[Any], str],
    content_graph_search: Callable[[str, Any], str],
) -> str:
    """Multi-connection ``graph_search`` path: implicit content-graph union
    or an explicit cross-repo fan-out, formatted into one labeled block."""
    is_implicit_target = connection is None or (
        isinstance(connection, str) and connection.strip().lower() in ("", "default")
    )
    if is_implicit_target:
        results, fan_errors = _graph_search_implicit_fanout(
            entries, run_search, content_graph_search
        )
    else:
        # Explicit cross-repo fan-out — per-target timeout at the full budget so
        # one slow backend can't stall the set.
        results, fan_errors = engine_routing.fanout_execute(
            entries, lambda name, engine: run_search(engine)
        )
    out_lines = [f"=== {name} ===\n{results[name]}" for name in results]
    out_lines += [
        f"=== {name} (error) ===\n{err}"
        for name, err in {**errors, **fan_errors}.items()
    ]
    out_lines.append(f"[connection={connection or 'default'} graph=(none — fan-out)]")
    return "\n\n".join(out_lines)


def register_evidence_tools(
    mcp, tools: ToolTable, engine_provider: Callable[[], Any]
) -> None:
    """Register the evidence_tools tools and their REST twins."""

    @mcp.tool(
        name="graph_search",
        description="Search the Knowledge Graph using multiple strategies (hybrid, concept, analogy, memory, discover, dci).",
        tags=["arbora", "search"],
    )
    async def graph_search(
        query: str = Field(description="Natural language search query or concept ID."),
        mode: str = Field(
            default="hybrid",
            description="Search strategy:\n- 'hybrid': Semantic + keyword weighted search (default).\n- 'hyde': Memory-first HyDE multi-query plan + dual threshold (CONCEPT:MERISTEM-KG.retrieval.self-correcting-second-pass).\n- 'deep': Wide-recall single query at the 0.28 deep threshold.\n- 'concept': Look up a CONCEPT:ID (e.g. 'MERISTEM-KG.query.vendor-agnostic-traversal', 'MERISTEM-ORCH.execution.inject-signal-board-observations').\n- 'analogy': Find structurally similar concepts.\n- 'memory': Search tiered memory (episodic/semantic/procedural).\n- 'discover': Cross-reference query against all ingested content.\n- 'dci': Direct Corpus Interaction.\n- 'latent': Latent-topology hierarchical routing (CONCEPT:MERISTEM-KG.memory.auto-similarity-memory-graph).\n- 'sira': Single-shot SIRA sparsity-aligned context.\n- 'hard_negatives': Mine hard negatives for the query (CONCEPT:MERISTEM-KG.memory.auto-similarity-memory-graph).\n- 'rerank': Hybrid semantic+keyword re-scoring of candidates.\n- 'adore': Iterative query expansion with retrieval-grounded graded relevance feedback + training-free stopping (CONCEPT:MERISTEM-KG.query.adore-concept-expansion/2.87).\n- 'chrono_ids': Attach an explicit temporal semantic ID (+recency bucket) to each result for generative retrieval (CONCEPT:MERISTEM-KG.query.chronoid-fits-residual-quantization).\n- 'compiled': Policy-aware ``ContextCompiler`` bundle (CONCEPT:MERISTEM-KG.retrieval.context-compiler) — MMR-diversified, evidence/freshness-weighted, token-budgeted, policy-filtered context with citations + a proof graph, instead of the plain relevance-sorted text the other modes return.",
        ),
        top_k: int = Field(default=10, description="Maximum results to return."),
        self_correct: bool = Field(
            default=False,
            description="CONCEPT:MERISTEM-KG.retrieval.self-correcting-second-pass — run a self-correcting second retrieval pass at the deep threshold when the quality gate fails.",
        ),
        as_of: str = Field(
            default="",
            description="Optional ISO-8601 instant. Pack-driven recency decay is measured relative to this time, enabling knowledge-state-as-of-date-D retrieval such as an academic literature state. Defaults to now (CONCEPT:MEDULLA-KG.compute.rust-native-training-loss).",
        ),
        connection: str = Field(
            default="",
            description=(
                "CONCEPT:MERISTEM-KG.backend.multi-connection-registry — named BACKEND connection to search "
                "(default = primary). Use a registered connection name, or 'all' "
                "(or a comma-separated list) to fan out and get per-connection "
                "labeled results. Selects WHICH BACKEND, never which physical "
                "graph — see `graph`."
            ),
        ),
        graph: str = Field(
            default="",
            description=(
                "CONCEPT:MERISTEM-KG.backend.explicit-graph-selection — explicit physical engine graph, independent of "
                "`connection`. Empty = the caller's own bound graph. Requires "
                "exactly one resolved `connection`; fails closed (typed error, "
                "never a silent default/union) on an unknown graph or a fan-out/"
                "unsupported-connection combination. Echoed in the response."
            ),
        ),
        token_budget: int = Field(
            default=0,
            description=(
                "mode='compiled' only: token budget the assembled bundle must fit "
                "inside (CONCEPT:MERISTEM-KG.retrieval.context-compiler). 0 uses the compiler's default budget."
            ),
        ),
    ) -> str:
        """Search the Knowledge Graph using multiple strategies. Useful for finding context, concepts, memories, and capabilities across the ecosystem."""
        # See `_run_graph_query`'s identical normalization: a direct call
        # bypassing `_execute_tool` binds an omitted `graph` to a raw, truthy
        # `FieldInfo` rather than `""`.
        graph = graph if isinstance(graph, str) else ""

        def _execute() -> str:
            def _run_search(engine: Any) -> str:
                if not engine:
                    return "Error: IntelligenceGraphEngine not active."
                try:
                    return _search_with_engine(engine)
                except Exception as e:
                    logger.warning("operation failed", exc_info=True)
                    return public_error_text(e)

            def _content_graph_search(name: str, engine: Any) -> str:
                # B-18 fix: mirrors `_run_graph_query`'s `_content_graph_query`
                # above — within the implicit content-graph UNION fan-out,
                # `name` is the physical graph `engine` is scoped to
                # (`ingest_routing.safe_engine_for_graph`'s `for_graph()` view
                # never rebinds `session.graph`), so the wire layer's mismatch
                # lock rejected every non-"default" leg. Narrow the verified
                # session to the same graph via the sanctioned
                # `bound_to_graph` primitive before running the search.
                # `_run_search` never raises (it self-catches into an error
                # string) — match that contract so `bound_to_graph` itself
                # failing (e.g. no ambient session) degrades the same way
                # instead of escaping this helper uncaught.
                try:
                    with engine_routing.bound_to_graph(name):
                        return _run_search(engine)
                except Exception as e:
                    logger.warning("operation failed", exc_info=True)
                    return public_error_text(e)

            def _search_with_engine(engine: Any) -> str:
                # CONCEPT:MERISTEM-KG.retrieval.acl-aware-vector-retrieval — every served
                # `graph_search` call already runs inside the middleware-minted
                # ambient GraphSession (`verified_tool_session_scope`). Passing it
                # explicitly opts THIS served path into `search_hybrid`'s per-node
                # ACL + owner/scope + audit enforcement (a no-op for internal
                # callers that pass no session — see `search_hybrid`'s docstring),
                # closing the gap where vector/hybrid results reached an external
                # caller completely unfiltered, unlike the guarded `graph_query`
                # Cypher path.
                from arbora.security.session import (
                    current_session,
                )

                _session = current_session()
                if mode in _GRAPH_SEARCH_RESULTS_MODES:
                    results = _graph_search_produce_results(
                        engine,
                        _session,
                        mode=mode,
                        query=query,
                        top_k=top_k,
                        self_correct=self_correct,
                        as_of=as_of,
                    )
                elif mode == "hard_negatives":
                    return _graph_search_hard_negatives(engine, query)
                elif mode == "compiled":
                    return _graph_search_compiled(
                        engine,
                        query,
                        top_k=top_k,
                        as_of=as_of,
                        token_budget=token_budget,
                    )
                else:
                    return f"Error: Unknown search mode '{mode}'"

                if not results:
                    return f"No results found for query: '{query}'"
                return _graph_search_format_results(results)

            # CONCEPT:MERISTEM-KG.backend.multi-connection-registry — resolve the connection(s). CONCEPT:MERISTEM-KG.ingest.unified-query-routing — an
            # implicit-default search fans across the active content-graph set so content
            # routed to ``code:*``/``src:*`` graphs stays findable as one KG.
            # CONCEPT:MERISTEM-KG.backend.explicit-graph-selection — `graph` requires exactly one resolved
            # connection, so it fails closed against any fan-out (explicit or the
            # implicit content-graph union) — never a silent ignore or union.
            targets = engine_routing.resolve_graph_targets(
                engine_routing._resolve_read_engines, connection, graph
            )
            if isinstance(targets, str):
                return targets
            entries, errors, fanout = targets

            if not fanout:
                return _graph_search_single_target(entries, graph, _run_search)

            return _graph_search_fanout(
                entries, errors, connection, _run_search, _content_graph_search
            )

        return await run_blocking_ordered(_execute)

    tools["graph_search"] = graph_search

    @mcp.tool(
        name="graph_search_synthesis",
        description=(
            "Synthesize a shortcut-resistant deep-search task from the evidence graph, "
            "or diagnose realized search difficulty of solver trajectories "
            "(CONCEPT:MERISTEM-KG.retrieval.evidence-graph-workspace/2.71/2.72, AHE-3.30; distills arXiv:2606.12087)."
        ),
        tags=["arbora", "search", "synthesis", "training-data"],
    )
    def graph_search_synthesis(
        action: str = Field(
            default="synthesize",
            description=(
                "'synthesize': build an evidence subgraph around an answer entity and "
                "formulate + adversarially refine a question that forces multi-hop "
                "search (no exposed constants / single-clue / co-coverage shortcuts). "
                "'diagnose': score solver trajectories with the FORT signatures "
                "(solving cost, answer hit time, prior-shortcut rate) + a search-heavy "
                "verdict."
            ),
        ),
        hops: int = Field(
            default=2, description="action=synthesize — evidence-graph BFS depth."
        ),
        fanout: int = Field(
            default=8,
            description="action=synthesize — max neighbors expanded per node.",
        ),
    ) -> str:
        """Shortcut-resistant search-task synthesis and realized-difficulty diagnosis."""

        raise not_served("graph.training_signals", "MEDULLA-DECISION-ENGINE-R751")

    tools["graph_search_synthesis"] = graph_search_synthesis

    def _graph_document_tree_build(
        document_id: str,
        text: str,
        persist: bool,
        thin: bool,
        summarize: bool,
        engine: Any,
    ) -> str:
        raise not_served(
            "knowledge_graph.ontology.document_processing", "MEDULLA-REPO-INGEST-R652"
        )

    def _graph_document_tree_structure(document_id: str, engine: Any) -> str:
        raise not_served(
            "knowledge_graph.retrieval.hierarchical_document_retriever",
            "MEDULLA-FEDERATED-QUERY-R653",
        )

    def _graph_document_tree_content(document_id: str, ranges: str, engine: Any) -> str:
        raise not_served(
            "knowledge_graph.retrieval.hierarchical_document_retriever",
            "MEDULLA-FEDERATED-QUERY-R653",
        )

    def _graph_document_tree_retrieve(
        query: str,
        text: str,
        document_id: str,
        top_k: int,
        use_llm: bool,
        engine: Any,
    ) -> str:
        raise not_served(
            "knowledge_graph.ontology.document_processing", "MEDULLA-REPO-INGEST-R652"
        )

    def _graph_document_tree_spine_resolve(
        engine: Any, action: str, text: str, artifact_id: str, document_id: str
    ) -> tuple[Sequence[Any], str, str | None]:
        raise not_served(
            "knowledge_graph.ingestion.evidence_spine", "MEDULLA-REPO-INGEST-R002"
        )

    def _graph_document_tree_spine_cite(
        fragments: Sequence[Any],
        resolved_artifact: str,
        fragment_id: str,
        content_hash: str,
    ) -> str:
        raise not_served(
            "knowledge_graph.ingestion.evidence_spine", "MEDULLA-REPO-INGEST-R002"
        )

    def _graph_document_tree_spine_fragments(
        fragments: Sequence[Any], resolved_artifact: str, document_id: str, kinds: str
    ) -> str:
        wanted = {k.strip() for k in kinds.split(",") if k.strip()}
        selected = [f for f in fragments if not wanted or f.kind in wanted]
        return json.dumps(
            {
                "action": "fragments",
                "artifact_id": resolved_artifact,
                "document_id": document_id,
                "fragment_count": len(selected),
                "fragments": [
                    {
                        "fragment_id": f.fragment_id,
                        "address": f.address,
                        "kind": f.kind,
                        "content_hash": f.content_hash,
                        "version_id": f.version_id,
                        "sequence": f.sequence,
                        "ordinal": f.ordinal,
                        "depth": f.depth,
                        "parent_fragment_id": f.parent_fragment_id,
                        "char_start": f.char_start,
                        "char_end": f.char_end,
                        "text": f.text,
                    }
                    for f in selected
                ],
            },
            default=str,
        )

    def _graph_document_tree_spine(
        action: str,
        engine: Any,
        text: str,
        artifact_id: str,
        document_id: str,
        fragment_id: str,
        content_hash: str,
        kinds: str,
    ) -> str:
        # ── evidence spine (CONCEPT:MERISTEM-KG.ingest.stable-fragment-address) ──
        # The citation surface: 'fragments' lists what can be cited, 'cite'
        # answers whether an existing citation still holds. Both reach the
        # same core the ingest path writes, so the REST twin
        # (/graph/document-tree) gets them with no second implementation.
        fragments, resolved_artifact, error_response = (
            _graph_document_tree_spine_resolve(
                engine, action, text, artifact_id, document_id
            )
        )
        if error_response is not None:
            return error_response
        if action == "cite":
            return _graph_document_tree_spine_cite(
                fragments, resolved_artifact, fragment_id, content_hash
            )
        return _graph_document_tree_spine_fragments(
            fragments, resolved_artifact, document_id, kinds
        )

    # ══════════════════════════════════════════════════════════════════
    # graph_document_tree — CONCEPT:MERISTEM-KG.retrieval.section-tree +
    # CONCEPT:MERISTEM-KG.retrieval.tree-navigation. PageIndex-style map-then-fetch over
    # a document's section tree: build the tree, view the text-free structure,
    # fetch cited char/page ranges, or navigate the tree by relevance. Complements
    # (does not replace) graph_search's vector/community retrieval — route long
    # single-document queries here where "similar != relevant".
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_document_tree",
        description=(
            "Reasoning-tree (vectorless) document retrieval over a per-document "
            "section tree (CONCEPT:MERISTEM-KG.retrieval.section-tree/tree-navigation; "
            "distills PageIndex). action: 'build' (build + optionally persist the "
            "section tree from text or a stored document), 'structure' (return the "
            "text-free table-of-contents map = get_document_structure), 'content' "
            "(fetch section bodies for cited char ranges like '96..208,300..420' = "
            "get_page_content), 'retrieve' (walk the tree by relevance and return "
            "sections with cited start..end ranges), 'fragments' (the addressable "
            "evidence spine — every citable Fragment of an artifact/document with "
            "its stable address + content hash; CONCEPT:MERISTEM-KG.ingest.stable-fragment-address), "
            "'cite' (resolve a stored citation against the current artifact and "
            "report current | stale | moved | lost). Complements graph_search for "
            "long single documents where similar != relevant."
        ),
        tags=["arbora", "retrieval", "document", "tree"],
    )
    def graph_document_tree(
        action: str = Field(
            description="build | structure | content | retrieve | fragments | cite",
        ),
        document_id: str = Field(
            default="",
            description="Target Document id (structure/content, and build/retrieve when loading from the graph).",
        ),
        text: str = Field(
            default="",
            description="action=build/retrieve — inline document text (markdown) to build the tree from, instead of loading a stored document.",
        ),
        query: str = Field(
            default="",
            description="action=retrieve — the natural-language query to navigate the tree with.",
        ),
        ranges: str = Field(
            default="",
            description="action=content — char ranges to fetch, e.g. '96..208,300..420'.",
        ),
        top_k: int = Field(
            default=5, description="action=retrieve — max sections to return."
        ),
        persist: bool = Field(
            default=True,
            description="action=build — write the Section nodes/edges to the graph (requires document_id).",
        ),
        thin: bool = Field(
            default=True,
            description="action=build — collapse tiny sections into their parent (token-budget thinning).",
        ),
        summarize: bool = Field(
            default=True,
            description="action=build — compute per-node summaries for a text-free structure map.",
        ),
        use_llm: bool = Field(
            default=False,
            description="action=retrieve — try LLM tree navigation before the lexical walk.",
        ),
        artifact_id: str = Field(
            default="",
            description="action=fragments/cite — the source Artifact to read the evidence spine of (alternative to document_id).",
        ),
        fragment_id: str = Field(
            default="",
            description="action=cite — the stable fragment address the citation stored.",
        ),
        content_hash: str = Field(
            default="",
            description="action=cite — the 'sha256:<hex>' content hash the citation stored, used to detect staleness and to relocate a moved fragment.",
        ),
        kinds: str = Field(
            default="",
            description="action=fragments — comma-separated fragment kinds to keep (heading,paragraph,table,table_row,list_item,code_block,quote).",
        ),
    ) -> str:
        """Map-then-fetch + tree-navigation retrieval over a document section tree."""
        engine = engine_provider()

        if action == "build":
            return _graph_document_tree_build(
                document_id, text, persist, thin, summarize, engine
            )
        if action == "structure":
            return _graph_document_tree_structure(document_id, engine)
        if action == "content":
            return _graph_document_tree_content(document_id, ranges, engine)
        if action == "retrieve":
            return _graph_document_tree_retrieve(
                query, text, document_id, top_k, use_llm, engine
            )
        if action in {"fragments", "cite"}:
            return _graph_document_tree_spine(
                action,
                engine,
                text,
                artifact_id,
                document_id,
                fragment_id,
                content_hash,
                kinds,
            )

        return json.dumps(
            {
                "error": f"unknown action '{action}' "
                "(expected build|structure|content|retrieve|fragments|cite)"
            }
        )

    tools["graph_document_tree"] = graph_document_tree
