"""The engine client's domain sub-clients and the methods each serves (ARBORA-HOST-R001.8).

Pure introspection of the ``medulla`` client: the host's engine tools and the doctor both read
the same domain list, so it lives below both.
"""

from __future__ import annotations

import inspect
import logging

__all__ = ["DOMAIN_CLASSES", "discover_domains"]

logger = logging.getLogger(__name__)

# ── Domain → client sub-client class map ─────────────────────────────────────
# domain name == the attribute on ``SyncMedullaClient`` == the REST sub-path.
# The class is introspected (below) for its public async methods = the actions.
DOMAIN_CLASSES: dict[str, str] = {
    "nodes": "NodeClient",
    "edges": "EdgeClient",
    "graph": "GraphOperationsClient",
    "analytics": "AnalyticsClient",
    "lifecycle": "LifecycleClient",
    "reasoning": "ReasoningClient",
    "ledger": "LedgerClient",
    "channels": "ChannelsClient",
    "tenants": "MultiTenantClient",
    "resharding": "ReshardingClient",
    "placement": "PlacementClient",
    "consensus": "ConsensusClient",
    "finance": "FinanceClient",
    "datascience": "DataScienceClient",
    "mining": "MiningClient",
    "query": "QueryClient",
    "txn": "TxnClient",
    "timeseries": "TimeSeriesClient",
    "rdf": "RdfClient",
    "streaming": "StreamingClient",
    "blob": "BlobClient",
    # MERISTEM-P0-6: previously-missing namespaces (audited gap #3). Domain name ==
    # the attribute on ``SyncMedullaClient`` (checked against the
    # installed ``medulla.client`` module — NOT a guess): the graph-
    # learning sub-client is exposed as ``.graphlearn`` (no underscore), so
    # the domain/tool/REST-path name below is ``graphlearn`` to match it.
    # rbac/admin are ADMIN_DOMAINS (see below) — gated BEFORE being newly
    # exposed, per the audit's explicit ordering.
    "broker": "BrokerClient",
    "rbac": "RbacClient",
    "admin": "AdminClient",
    "graphlearn": "GraphLearnClient",
    # D-VZ-1 (lane w6-viz-xy) — native visualization: render a ViewSpec through
    # the eg-viz ColumnStore/LOD/export pipeline (``Method::Viz``). Degrades
    # cleanly (see ``discover_domains`` below) until the engine client ships
    # ``VizClient``.
    "viz": "VizClient",
    # B-12 — the native document/image/audio/video ingest + typed-query +
    # events surface (16 methods: ingest/ingest_stream/query_image_region/
    # query_similar_images/query_audio_window/query_video_window/delete/
    # move_to_cold/restore/events/stats/collect_tombstones/capabilities/…)
    # had zero au callers anywhere, verified by grep, despite being fully
    # implemented and bound on the typed client as ``.modalities``. This is
    # the same generic introspection-driven registration every other domain
    # gets — no bespoke tool needed.
    "modalities": "ServedModalityClient",
}


def discover_domains() -> dict[str, list[str]]:
    """Introspect the engine client sub-client classes → ``{domain: [methods]}``.

    The ``medulla`` client mirrors the wire protocol 1:1, so its public
    async methods ARE the engine's invocable capability surface. Discovering them
    (instead of hand-listing 200+ methods) keeps this surface drift-free: a new
    engine method shows up automatically once the client wraps it.
    """
    try:
        from medulla import client as _client_mod
    except (ImportError, OSError) as exc:  # engine client absent ⇒ register nothing
        # Degrade gracefully (a deployment with no engine client bound yet — e.g.
        # `tiny` pre-bootstrap — must not crash the whole MCP process just
        # because engine_<domain> introspection can't run), but LOUDLY: a silent
        # `{}` here previously dropped every engine_<domain> tool (and every
        # engine_<domain>_<method> verbose op scripts/gen_arbora_manifest.py
        # derives from this dict) with zero visible signal, which is exactly
        # what let a manifest regen on a box missing the `medulla`
        # wheel/kernel silently ship a manifest with hundreds fewer entries.
        logger.warning(
            "engine surface discovery: 'medulla' client unavailable — "
            "ALL engine_<domain> tools (and their verbose/manifest ops) will be "
            "absent this run (exception_type=%s). Install the engine with "
            "`pip install medulla` if this MCP process is meant to serve engine tools.",
            type(exc).__name__,
            exc_info=True,
        )
        return {}

    out: dict[str, list[str]] = {}
    for domain, class_name in DOMAIN_CLASSES.items():
        cls = getattr(_client_mod, class_name, None)
        if cls is None:
            continue
        methods = sorted(
            name
            for name, member in inspect.getmembers(cls, inspect.iscoroutinefunction)
            if not name.startswith("_")
        )
        if methods:
            out[domain] = methods
    return out
