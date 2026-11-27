"""The kv cache and kv checkpoint engine-surface tools (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import base64
import json
import logging
from typing import Any

from pydantic import Field

from arbora.api.errors import not_served
from arbora.mcp_server import runtime
from arbora.mcp_server.backing.engine_surface_types import (
    KvCheckpointIntelligenceRequest,
)
from arbora.mcp_server.backing.tools.engine_surface_common import (
    _json_default,
    _surface_error,
)
from arbora.mcp_server.host_caches import ToolTable

logger = logging.getLogger(__name__)


def _kv_backend() -> Any:
    """Build the KG-2.306 KV-cache connector from the engine's MEDULLA-187 environment.

    Isolated behind this indirection so unit tests can inject a fake backend
    (CONCEPT:MERISTEM-KG.coordination.engine-message-broker).
    """
    raise not_served("kvcache", "MEDULLA-DURABLE-KERNEL-R800")


def _checkpoint_store(graph: str) -> Any:
    """Build a :class:`~meristem.kvcache.KVCheckpointStore` bound to the
    live engine client for ``graph`` (CONCEPT:MERISTEM-KG.memory.kv-checkpoint-resource).

    Isolated behind this indirection so unit tests can inject a fake compute
    object, matching every other resolver in this module.
    """
    raise not_served("kvcache.checkpoint", "MEDULLA-DURABLE-KERNEL-R800")


def _checkpoint_manager(graph: str) -> Any:
    """Build a :class:`~meristem.kvcache.TieredCheckpointManager` over the shared
    RAM tier, binding the durable tier when the engine is reachable.

    A durable store that cannot be built is NOT an error here: the manager works
    RAM-only and every promotion is refused with that reason, which is the correct
    degrade for a checkpoint layer whose default tier is RAM anyway.
    """
    raise not_served("kvcache.tiering", "MEDULLA-DURABLE-KERNEL-R800")


def _checkpoint_observation(bundles: dict[str, str]) -> Any:
    """Build the observation from the explicit fields plus any bundles handed in.

    An agent that has just run ``graph_ask`` / a context compile already holds the
    exact shapes the grounding, contradiction and novelty scorers want, so it can
    hand those straight over instead of transcribing four counts by hand. Explicit
    ``observation_json`` fields always win over anything derived from a bundle —
    the caller's direct measurement is more authoritative than an inference.
    """
    raise not_served("kvcache.worthiness", "MEDULLA-DURABLE-KERNEL-R800")


def _kv_ram_stats_response(action: str, manager: Any) -> str:
    """``action='ram_stats'`` — the RAM tier's occupancy plus what shaped it."""
    return json.dumps(
        {
            "surface": "kv_checkpoint",
            "action": action,
            "result": {
                **manager.ram_store.stats().model_dump(mode="json"),
                "eligibility_gate": manager.eligibility_gate.name,
                # Which signals are active in THIS deployment. Without it a score
                # is uninterpretable — an operator who removed a default scorer or
                # added their own has no other way to see what produced the number.
                "scorers": [
                    {"name": s.name, "weight": s.weight}
                    for s in manager.advisor.registry.scorers()
                ],
                "ram_threshold": manager.advisor.ram_threshold,
            },
        }
    )


def _kv_recommend_response(action: str, manager: Any, bundles: dict[str, str]) -> str:
    """``action='recommend'`` — a scored, advisory-only worthiness verdict."""
    try:
        observation = _checkpoint_observation(bundles)
    except Exception as exc:
        logging.getLogger(__name__).debug("caller-supplied JSON/shape", exc_info=True)
        return _surface_error(
            exc, surface="kv_checkpoint", action=action, code="invalid_request"
        )
    recommendation = manager.recommend(observation)
    return json.dumps(
        {
            "surface": "kv_checkpoint",
            "action": action,
            "result": {
                **recommendation.model_dump(mode="json", exclude={"observation"}),
                "advisory": recommendation.as_advisory(),
            },
        },
        default=_json_default,
    )


def _kv_explain_response(
    action: str, manager: Any, checkpoint_id: str, requesting_tenant: str
) -> str:
    """``action='explain'`` — why this checkpoint exists and why it is where it is."""
    raise not_served("kvcache.checkpoint", "MEDULLA-DURABLE-KERNEL-R800")


def _kv_promote_response(
    action: str,
    manager: Any,
    checkpoint_id: str,
    requesting_tenant: str,
    trigger: str,
) -> str:
    """``action='promote'`` — the gated RAM→disk promotion."""
    raise not_served("kvcache.checkpoint", "MEDULLA-DURABLE-KERNEL-R800")


def _kv_checkpoint_now(
    action: str, manager: Any, bundles: dict[str, str], req: dict[str, Any]
) -> str:
    """``action='checkpoint_now'`` — store to the RAM tier, optionally persist.

    Durable persistence always passes the authority-derived eligibility gate; the
    tenant it is authorized under comes from the verified session, never the payload.
    """
    raise not_served("kvcache.checkpoint", "MEDULLA-DURABLE-KERNEL-R800")


def _kv_checkpoint_intelligence(
    action: str, request: KvCheckpointIntelligenceRequest
) -> str:
    """The worthiness/tiering/eligibility half of ``graph_kv_checkpoint``.

    CONCEPT:MERISTEM-KG.memory.checkpoint-worthiness-scoring. Split out of the tool body so
    the three trigger paths can be exercised directly, and so the tool function stays
    argument-marshalling only.

    CONCEPT:MERISTEM-OS.governance.authority-derived-persistence-eligibility — **the tenant a
    durable write is authorized under is read from the verified session, never from the
    payload.** Before this, ``initiator="user", operator_grant=true`` were arguments an
    agent could simply assert, which made the deny-by-default gate defeatable by any
    caller that chose to lie. They are gone: ``trigger`` is now provenance only, and the
    authority comes from :func:`derive_caller_authority` over the ambient
    ``GraphSession``.
    """
    # Validate the trigger AT THE BOUNDARY. It is a Literal on PersistenceRequest /
    # RAMCheckpointRecord, so an unrecognized value would surface deep inside as a raw
    # pydantic ValidationError that the KVCheckpointError handlers below never catch.
    if request.trigger not in {"user", "agent", "system"}:
        return _surface_error(
            ValueError(
                f"trigger must be one of user|agent|system, got {request.trigger!r}"
            ),
            surface="kv_checkpoint",
            action=action,
            code="invalid_request",
        )

    manager = _checkpoint_manager(request.graph)
    bundles = {
        "observation_json": request.observation_json,
        "evidence_bundle_json": request.evidence_bundle_json,
        "context_bundle_json": request.context_bundle_json,
    }
    if action == "ram_stats":
        return _kv_ram_stats_response(action, manager)
    if action == "recommend":
        return _kv_recommend_response(action, manager, bundles)
    if action == "explain":
        return _kv_explain_response(
            action, manager, request.checkpoint_id, request.requesting_tenant
        )
    if action == "promote":
        return _kv_promote_response(
            action,
            manager,
            request.checkpoint_id,
            request.requesting_tenant,
            request.trigger,
        )
    # action == "checkpoint_now"
    return _kv_checkpoint_now(
        action,
        manager,
        bundles,
        {
            "data_b64": request.data_b64,
            "model_identity": request.model_identity,
            "quantization": request.quantization,
            "serving_engine": request.serving_engine,
            "engine_version": request.engine_version,
            "prefix_digest": request.prefix_digest,
            "tenant": request.tenant,
            "policy_version": request.policy_version,
            "run_id": request.run_id,
            "point": request.point,
            "trigger": request.trigger,
            "persist": request.persist,
            "sources_json": request.sources_json,
        },
    )


def _kvcache_get(backend: Any, action: str, key: str) -> str:
    """``graph_kvcache`` ``get`` — the block bytes, base64'd, or an explicit miss."""
    blob = backend.get(key)
    return json.dumps(
        {
            "surface": "kvcache",
            "action": action,
            "hit": blob is not None,
            "value_b64": (
                base64.b64encode(blob).decode("ascii") if blob is not None else None
            ),
        }
    )


def _kvcache_put(backend: Any, action: str, key: str, value_b64: str) -> str:
    """``graph_kvcache`` ``put`` — store base64-decoded block bytes under ``key``."""
    try:
        raw = base64.b64decode(value_b64) if value_b64 else b""
    except (ValueError, TypeError) as exc:
        return _surface_error(exc, surface="kvcache", code="invalid_request")
    return json.dumps(
        {
            "surface": "kvcache",
            "action": action,
            "stored": bool(backend.put(key, raw)),
        }
    )


def _kvcache_probe(backend: Any, action: str, key: str) -> str:
    """``graph_kvcache`` ``contains``/``exists`` — presence only, never the bytes."""
    probe = backend.exists if action == "exists" else backend.contains
    return json.dumps(
        {"surface": "kvcache", "action": action, "present": bool(probe(key))}
    )


def _kvcache_stats(backend: Any, action: str) -> str:
    """``graph_kvcache`` ``stats`` — occupancy + dedup counters."""
    stats = backend.stats()
    data = stats.model_dump() if hasattr(stats, "model_dump") else dict(stats)
    return json.dumps(
        {"surface": "kvcache", "action": action, "result": data},
        default=_json_default,
    )


def _kvcache_dispatch(backend: Any, action: str, key: str, value_b64: str) -> str:
    """Route one ``graph_kvcache`` action onto the KG-2.306 connector.

    The key-required guard covers exactly the four key-addressed actions, so an
    unknown action still reports itself as unknown rather than as a missing key.
    """
    if action == "stats":
        return _kvcache_stats(backend, action)
    if action not in {"get", "put", "contains", "exists"}:
        return json.dumps({"surface": "kvcache", "error": f"unknown action {action!r}"})
    if not key:
        return json.dumps({"surface": "kvcache", "error": "key required"})
    if action == "get":
        return _kvcache_get(backend, action, key)
    if action == "put":
        return _kvcache_put(backend, action, key, value_b64)
    return _kvcache_probe(backend, action, key)


def _kvcache_close(backend: Any) -> None:
    """Best-effort connector cleanup — never raises."""
    close = getattr(backend, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # best-effort cleanup
            logger.debug("kv-cache backend close failed", exc_info=True)


def _kv_create_checkpoint(action: str, store: Any, req: dict[str, Any]) -> str:
    """``graph_kv_checkpoint`` ``create`` — the full key plus the blob bytes."""
    raise not_served("kvcache", "MEDULLA-DURABLE-KERNEL-R800")


def _kv_instantiate_agent(
    action: str,
    store: Any,
    checkpoint_id: str,
    requesting_tenant: str,
    new_run_id: str,
    current_policy_version: str,
) -> str:
    """``graph_kv_checkpoint`` ``instantiate_agent`` — fail-closed load + lineage."""
    raise not_served("kvcache", "MEDULLA-DURABLE-KERNEL-R800")


def _kv_restore_conversation(
    action: str,
    store: Any,
    checkpoint_id: str,
    conversation_id: str,
    requesting_tenant: str,
    current_policy_version: str,
    allow_cold_start: bool,
) -> str:
    """``graph_kv_checkpoint`` ``restore_conversation`` — fail-closed load + lineage."""
    raise not_served("kvcache", "MEDULLA-DURABLE-KERNEL-R800")


def register_kv_cache_tools(mcp, tools: ToolTable) -> None:
    """Register the kv_cache_tools tools and their REST twins."""

    # ══════════════════════════════════════════════════════════════════
    # graph_kvcache — shared content-addressed KV-cache (MEDULLA-187 / KG-2.306)
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_kvcache",
        description=(
            "CONCEPT:MERISTEM-KG.coordination.engine-message-broker — the engine's shared, content-addressed KV-cache over "
            "the MEDULLA-187 HTTP surface, driven through the KG-2.306 "
            "MedullaKVBackend connector. Actions: 'get' (key → base64 block "
            "bytes or miss), 'put' (key + value_b64 → stored bool), 'contains'/'exists' "
            "(key → bool), 'stats' (occupancy + dedup counters). The connector already "
            "degrades every transport error to a cache miss, so this tool never raises."
        ),
        tags=["arbora", "engine", "kvcache"],
    )
    def graph_kvcache(
        action: str = Field(
            default="stats", description="get | put | contains | exists | stats"
        ),
        key: str = Field(
            default="", description="Opaque block key (get/put/contains/exists)."
        ),
        value_b64: str = Field(
            default="", description="Base64-encoded block bytes to store (put)."
        ),
    ) -> str:
        """Thin wrapper over the KG-2.306 KV-cache connector (CONCEPT:MERISTEM-KG.coordination.engine-message-broker)."""
        try:
            backend = _kv_backend()
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "mis-config degrades, never raises", exc_info=True
            )
            return _surface_error(
                exc,
                surface="kvcache",
                action=action,
                code="dependency_unavailable",
            )
        try:
            return _kvcache_dispatch(backend, action, key, value_b64)
        finally:
            _kvcache_close(backend)

    tools["graph_kvcache"] = graph_kvcache

    # ══════════════════════════════════════════════════════════════════
    # graph_kv_checkpoint — KV-cache checkpoints as graph resources
    # (CONCEPT:MERISTEM-KG.memory.kv-checkpoint-resource)
    # ══════════════════════════════════════════════════════════════════
    @mcp.tool(
        name="graph_kv_checkpoint",
        description=(
            "CONCEPT:MERISTEM-KG.memory.kv-checkpoint-resource — checkpoint a KV-cache at a point of "
            "ideal understanding, then initialise a new agent from it or reset a "
            "conversation back to it. The heavy KV blob lives behind the engine's "
            "content-addressed blob store; the response never inlines it — only "
            "provenance (checkpoint_id/digest/blob_id) so a caller with real access to "
            "the blob store can fetch it directly. Actions: 'create' (data_b64 + the "
            "full key: model_identity/quantization/serving_engine/engine_version/"
            "prefix_digest/tenant/policy_version, plus run_id/point) → checkpoint "
            "provenance; 'instantiate_agent' (checkpoint_id + requesting_tenant + "
            "new_run_id) → fail-closed load + an initializedFrom lineage edge from a "
            "fresh AgentRun node; 'restore_conversation' (checkpoint_id + "
            "conversation_id + requesting_tenant) → fail-closed load + a restoredFrom "
            "lineage edge, or set allow_cold_start=true for an EXPLICIT, traced "
            "cold-start fallback instead of raising. A tenant mismatch or a stale "
            "policy_version is ALWAYS refused (cross-tenant checkpoint reuse is a "
            "security boundary, never bypassable). "
            "CONCEPT:MERISTEM-KG.memory.checkpoint-worthiness-scoring adds the intelligence "
            "actions: 'recommend' (observation_json → a scored checkpoint-worthiness "
            "verdict with drivers, blockers and the scorers that had no evidence — "
            "advisory only, it takes no checkpoint); 'checkpoint_now' (data_b64 + the "
            "key; stores to the RAM tier immediately, and with persist=true ALSO "
            "attempts a durable write); 'promote' (checkpoint_id → RAM→disk "
            "promotion); 'explain' (checkpoint_id → why this checkpoint exists, why it "
            "is where it is, and the full authority derivation behind that); "
            "'ram_stats'. Durable persistence ALWAYS passes the "
            "CONCEPT:MERISTEM-OS.governance.checkpoint-persistence-eligibility gate, which "
            "(CONCEPT:MERISTEM-OS.governance.authority-derived-persistence-eligibility) "
            "DERIVES the verdict from YOUR verified session and delegation chain "
            "against the labels of every contributing source — most-restrictive "
            "composition, into your own tenancy only. There is no grant flag to set: an "
            "agent whose delegated authority already covers the material persists "
            "automatically, one whose does not is refused with the offending source or "
            "missing label named. An undeclared source set, or a source missing a "
            "classification/residency/retention label, DENIES. A checkpoint already "
            "living in RAM is NOT consent to write it to disk."
        ),
        tags=["arbora", "engine", "kvcache", "checkpoint", "memory"],
    )
    def graph_kv_checkpoint(
        action: str = Field(
            default="", description="create | instantiate_agent | restore_conversation"
        ),
        data_b64: str = Field(
            default="", description="Base64 KV-cache blob bytes (create only)."
        ),
        model_identity: str = Field(default="", description="create: key component."),
        quantization: str = Field(default="", description="create: key component."),
        serving_engine: str = Field(default="", description="create: key component."),
        engine_version: str = Field(default="", description="create: key component."),
        prefix_digest: str = Field(default="", description="create: key component."),
        tenant: str = Field(
            default="", description="create: owning tenant (mandatory key component)."
        ),
        policy_version: str = Field(
            default="", description="create: key component (defaults to '')."
        ),
        run_id: str = Field(default="", description="create: the source run's id."),
        point: str = Field(
            default="", description="create: label for the checkpointed point."
        ),
        provenance_json: str = Field(
            default="{}", description="create: free-form provenance JSON object."
        ),
        checkpoint_id: str = Field(
            default="",
            description="instantiate_agent/restore_conversation: the checkpoint to load.",
        ),
        requesting_tenant: str = Field(
            default="",
            description="instantiate_agent/restore_conversation: the caller's tenant "
            "(checked against the checkpoint's stored tenant, fail-closed).",
        ),
        current_policy_version: str = Field(
            default="",
            description="instantiate_agent/restore_conversation: current policy "
            "version to validate against (empty ⇒ skip the staleness check).",
        ),
        new_run_id: str = Field(
            default="", description="instantiate_agent: the new run's id."
        ),
        conversation_id: str = Field(
            default="", description="restore_conversation: the conversation to reset."
        ),
        allow_cold_start: bool = Field(
            default=False,
            description="restore_conversation: on an invalid/foreign/stale "
            "checkpoint, return an explicit traced cold-start result instead of "
            "raising.",
        ),
        observation_json: str = Field(
            default="{}",
            description="recommend/checkpoint_now: a CheckpointObservation as JSON "
            "(rebuild cost, sibling/queued tasks, retrieved/novel items, claim/"
            "evidence counts, contradictions, churn, phase, model_self_report). Every "
            "field is optional; an omitted field means NOT MEASURED and its scorer "
            "abstains rather than guessing.",
        ),
        evidence_bundle_json: str = Field(
            default="{}",
            description="recommend/checkpoint_now: an EvidenceBundle as JSON (as "
            "returned by graph_ask/graph_query). Its claims/evidence_spans/"
            "contradictions populate the grounding and contradiction axes directly, so "
            "a caller need not transcribe the counts. Explicit observation_json fields "
            "always win over anything derived here.",
        ),
        context_bundle_json: str = Field(
            default="{}",
            description="recommend/checkpoint_now: a ContextBundle as JSON. Its items/"
            "dropped_redundant/citations populate the retrieval-saturation (novelty) "
            "axis. Same precedence rule as evidence_bundle_json.",
        ),
        sources_json: str = Field(
            default="[]",
            description="checkpoint_now: the sources that contributed to this context, "
            "as a JSON list of either ref strings (resolved to their governance labels) "
            "or fully-labelled objects (source_id/classification/residency_regions/"
            "retention_days/markings). Usually unnecessary: citations carried on "
            "context_bundle_json already name them. An EMPTY source set refuses durable "
            "persistence — an unprovenanced checkpoint cannot be shown to permit "
            "data-at-rest.",
        ),
        trigger: str = Field(
            default="agent",
            description="checkpoint_now/promote: which path took this checkpoint — "
            "'user', 'agent' or 'system'. PROVENANCE ONLY. It is recorded on the audit "
            "record and never read by the eligibility decision, which derives authority "
            "from your verified session instead.",
        ),
        persist: bool = Field(
            default=False,
            description="checkpoint_now: also attempt durable (cross-session) "
            "persistence. Always subject to the eligibility gate; a refusal still "
            "leaves the RAM checkpoint in place.",
        ),
        graph: str = Field(
            default="", description="Target graph (default engine graph)."
        ),
    ) -> str:
        """Thin verb over :class:`~meristem.kvcache.KVCheckpointStore` (CONCEPT:MERISTEM-KG.memory.kv-checkpoint-resource)
        and :class:`~meristem.kvcache.TieredCheckpointManager`
        (CONCEPT:MERISTEM-KG.memory.checkpoint-worthiness-scoring)."""
        # ── the intelligence actions: worthiness, tiering, eligibility ──────
        # These route through TieredCheckpointManager (shared RAM tier) rather than
        # the durable store directly, because the RAM tier is the DEFAULT and disk is
        # a gated promotion off it.
        if action in {
            "recommend",
            "checkpoint_now",
            "promote",
            "explain",
            "ram_stats",
        }:
            intelligence_request = KvCheckpointIntelligenceRequest(
                graph=graph,
                data_b64=data_b64,
                model_identity=model_identity,
                quantization=quantization,
                serving_engine=serving_engine,
                engine_version=engine_version,
                prefix_digest=prefix_digest,
                tenant=tenant,
                policy_version=policy_version,
                run_id=run_id,
                point=point,
                checkpoint_id=checkpoint_id,
                requesting_tenant=requesting_tenant,
                observation_json=observation_json,
                evidence_bundle_json=evidence_bundle_json,
                context_bundle_json=context_bundle_json,
                sources_json=sources_json,
                trigger=trigger,
                persist=persist,
            )
            return _kv_checkpoint_intelligence(action, intelligence_request)

        try:
            store = _checkpoint_store(graph)
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "engine down is a normal degrade", exc_info=True
            )
            return _surface_error(
                exc,
                surface="kv_checkpoint",
                action=action,
                code="dependency_unavailable",
            )

        if action == "create":
            return _kv_create_checkpoint(
                action,
                store,
                {
                    "data_b64": data_b64,
                    "provenance_json": provenance_json,
                    "model_identity": model_identity,
                    "quantization": quantization,
                    "serving_engine": serving_engine,
                    "engine_version": engine_version,
                    "prefix_digest": prefix_digest,
                    "tenant": tenant,
                    "policy_version": policy_version,
                    "run_id": run_id,
                    "point": point,
                },
            )
        if action == "instantiate_agent":
            return _kv_instantiate_agent(
                action,
                store,
                checkpoint_id,
                requesting_tenant,
                new_run_id,
                current_policy_version,
            )
        if action == "restore_conversation":
            return _kv_restore_conversation(
                action,
                store,
                checkpoint_id,
                conversation_id,
                requesting_tenant,
                current_policy_version,
                allow_cold_start,
            )
        return json.dumps(
            {"surface": "kv_checkpoint", "error": f"unknown action {action!r}"}
        )

    tools["graph_kv_checkpoint"] = graph_kv_checkpoint

    runtime.ACTION_TOOL_ROUTES["graph_kv_checkpoint"] = "/graph/kv_checkpoint"

    runtime.ACTION_TOOL_ROUTES["graph_kvcache"] = "/graph/kvcache"
