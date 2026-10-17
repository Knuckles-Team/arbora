"""Pydantic request/response models for the core ``/api/graph/*`` REST surface
plus its non-graph siblings (SCHEMA LANE 3/3).

``runtime._mount_rest_routes`` mounts these routes with raw Starlette
``app.add_route(...)`` (see ``arbora/mcp_server/runtime.py``), which carries
no request/response schema at all — the live OpenAPI 3.1.0 spec documents
**zero** ``/api/graph/*`` paths as a result. This module is the typed
prerequisite for wiring ``response_model=``/body-typed parameters onto those
routes (a later, single-owner lane); it does not touch ``runtime.py`` itself.

Every model here was derived by reading the actual handler in
``arbora/mcp_server/runtime.py`` and, where the handler dispatches through
``_execute_tool("<tool>", **body)``, the real target tool signature in
``REGISTERED_TOOLS`` (``arbora/mcp_server/backing/tools/*.py``) — never invented.
See each model's docstring for its source handler/tool and the exact
permissiveness contract (``extra="allow"`` vs ``extra="forbid"``) proven
against that handler's code, not assumed.

The legacy ``/tools`` inventory routes are intentionally absent. Browser
catalogs use the typed EG-backed enhanced catalog owned by Arbora.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.evidence import EvidenceBundle
from arbora.gateway.schemas.connector import (
    ConnectorRunRequest,
    ConnectorRunResponse,
    ConnectorRunResult,
    ConnectorSourcesResponse,
    ConnectorSourcesResult,
)

__all__ = [
    # graph_query / graph_query/federated
    "GraphQueryFederatedRequest",
    "EvidenceBundleEnvelope",
    # graph_code / graph_research / graph_evaluate / graph_explain
    "GraphCodeRequest",
    "GraphResearchRequest",
    "GraphEvaluateRequest",
    "GraphExplainRequest",
    # graph_configure (generic + the three bespoke granular twins)
    "GraphConfigureRequest",
    "GraphConfigureResponse",
    "GraphConfigureSecretRequest",
    "SetSecretResult",
    "GraphConfigureSecretResponse",
    "GraphConfigureVaultSyncRequest",
    "VaultSyncResult",
    "GraphConfigureVaultSyncResponse",
    "HookDoctorEntry",
    "GraphConfigureDoctorResponse",
    # /goals + /goals/{goal_id}/cancel + /goals/{goal_id}/iterations
    "GoalRecord",
    "CreateGoalRequest",
    "CreateGoalResponse",
    "ListGoalsResponse",
    "CancelGoalResponse",
    # /connector/run + /connector/sources
    "ConnectorRunRequest",
    "ConnectorRunResult",
    "ConnectorRunResponse",
    "ConnectorSourcesResult",
    "ConnectorSourcesResponse",
    # /sessions
    "SessionRecord",
    "SessionsListQuery",
]


# ══════════════════════════════════════════════════════════════════
# Shared envelope
# ══════════════════════════════════════════════════════════════════


class EvidenceBundleEnvelope(BaseModel):
    """The ``{"status": "success", "result": <EvidenceBundle>}`` envelope shared
    by ``/graph/query``, ``/graph/query/federated``, ``/graph/code``,
    ``/graph/research``, ``/graph/evaluate``, and ``/graph/explain``.

    Every one of those routes dispatches a tool whose declared return type is
    :class:`~arbora.api.evidence.EvidenceBundle`; the
    handler serializes it with ``safe_json_load``, which calls
    ``.model_dump()`` on a Pydantic model rather than round-tripping through
    JSON text — so ``result`` is exactly an ``EvidenceBundle`` dump, not a
    freeform dict. Reused verbatim here (not redefined) per the "share one
    definition across identical shapes" rule — see
    ``arbora/api/evidence/bundle.py`` for the fully-described
    field set.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(
        description="Always 'success' on this path; failures return a "
        "different, non-2xx error body (see `_external_error_response`) "
        "and never reach this envelope shape."
    )
    result: EvidenceBundle = Field(
        description="The dispatched tool's EvidenceBundle answer, dumped to "
        "a plain dict by the handler's `safe_json_load(res)` call."
    )


# ══════════════════════════════════════════════════════════════════
# 1. graph_query / graph_query/federated
# ══════════════════════════════════════════════════════════════════


class GraphQueryFederatedRequest(BaseModel):
    """Request body for ``POST /graph/query/federated`` — a narrower,
    fixed-scope twin of ``graph_query`` (``graph_query_federated_endpoint`` in
    ``runtime.py``).

    ``extra="forbid"`` mirrors the handler's explicit allowlist for ``query``,
    ``params``, and ``reference_id``. Unknown fields fail before dispatch.

    Also unlike ``/graph/query``, ``params`` here is converted through
    ``_to_json_str`` server-side, so it genuinely accepts EITHER a JSON
    object or a pre-encoded string.
    """

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        default="",
        description="Read-only query string; always run with `scope='federated'` "
        "server-side (not caller-settable on this route).",
    )
    params: dict[str, Any] | str = Field(
        default_factory=dict,
        description="Query parameters as a JSON object OR a pre-encoded "
        "JSON string — the handler normalizes either via `_to_json_str` "
        "before dispatch (unlike the stricter `/graph/query`, which only "
        "accepts a pre-encoded string).",
    )
    reference_id: str = Field(
        default="",
        description="ExternalGraphReference id identifying the federated target.",
    )


# ══════════════════════════════════════════════════════════════════
# 2. graph_code / graph_research / graph_evaluate / graph_explain
#    (the focused analyze-suite tools — all share one action-core shape)
# ══════════════════════════════════════════════════════════════════

GraphCodeAction = Literal[
    "code_context",
    "cross_repo_usages",
    "call_graph",
    "similar_code",
    "routes",
    "change_coupling",
    "code_evolution",
    "blast_radius",
    "code_metrics",
    "arch_report",
    "adr",
]
GraphResearchAction = Literal[
    "synthesize",
    "deep_extract",
    "background_research",
    "relevance_sweep",
    "research_ingest",
    "evolve_variants",
    "track_citations",
    "spawn_background",
    "night_shift",
    "contradictions",
]
GraphEvaluateAction = Literal[
    "evaluate",
    "evaluate_alpha",
    "evaluate_harness",
    "guard_corpus",
    "harness_gate",
    "check_constraints",
    "specialize",
    "world_model_rollout",
    "latent_efficiency_benchmark",
    "assimilation_benchmark",
    "evolve_model",
    "evolve_code",
    "forecast",
    "causal",
    "invariant",
    "quant_crypto",
    "quant_exchange",
    "quant_microstructure",
    "quant_strategy",
    "quant_regime",
    "quant_insider",
]
GraphExplainAction = Literal["explain", "context", "executable_rag", "recommend"]


class GraphCodeRequest(BaseModel):
    """Request body for ``POST /graph/code`` — REST twin of the ``graph_code``
    MCP tool (``arbora/mcp_server/backing/tools/analyze_suite/py``), mounted via the
    shared ``_make_action_endpoint("graph_code")`` factory in ``runtime.py``.

    ``extra="forbid"``: PROVEN — the factory blind-splats ``**body`` into
    ``_execute_tool("graph_code", **body)``, but the tool function itself
    declares exactly ``action``/``query``/``top_k``/``node_id``/``depth``/
    ``target`` with no ``**kwargs`` catch-all, so
    ``_validate_tool_kwargs_against_signature`` raises ``UnsupportedToolFieldError``
    (mapped to a deterministic 400) for anything else — the "blind splat" is
    strict in practice, not permissive.
    """

    model_config = ConfigDict(extra="forbid")

    action: GraphCodeAction = Field(
        default="code_context",
        description="Code-intelligence operation to run.",
    )
    query: str = Field(
        default="", description="Question / area / symbol name / repo path."
    )
    top_k: int = Field(default=10, description="Result count.")
    node_id: str = Field(
        default="",
        description="Symbol/:Code node id (used by call_graph, similar_code, "
        "blast_radius).",
    )
    depth: int = Field(default=2, description="Traversal depth (blast_radius).")
    target: str = Field(
        default="",
        description="'how'|'usage'|'impact' (code_context) or "
        "'callees'|'callers'|'inherits' (call_graph).",
    )


class GraphResearchRequest(BaseModel):
    """Request body for ``POST /graph/research`` — REST twin of the
    ``graph_research`` MCP tool. Same factory/strictness proof as
    :class:`GraphCodeRequest` (``extra="forbid"``, verified against the tool's
    signature, no ``**kwargs``).
    """

    model_config = ConfigDict(extra="forbid")

    action: GraphResearchAction = Field(
        default="synthesize",
        description="Research/assimilation pipeline operation to run.",
    )
    query: str = Field(default="", description="Source / topic / artifact.")
    top_k: int = Field(default=10, description="Complexity budget / result count.")
    node_id: str = Field(default="", description="Optional node id.")
    depth: int = Field(default=2, description="Optional traversal depth.")
    target: str = Field(
        default="",
        description="Optional target (e.g. a local markdown vault root for "
        "'night_shift').",
    )


class GraphEvaluateRequest(BaseModel):
    """Request body for ``POST /graph/evaluate`` — REST twin of the
    ``graph_evaluate`` MCP tool. Same factory/strictness proof as
    :class:`GraphCodeRequest` (``extra="forbid"``).
    """

    model_config = ConfigDict(extra="forbid")

    action: GraphEvaluateAction = Field(
        default="evaluate",
        description="Evaluation / world-model / forecasting operation to run.",
    )
    query: str = Field(
        default="",
        description="Subject of the evaluation (JSON / id / start state).",
    )
    top_k: int = Field(default=10, description="Steps / result count.")
    node_id: str = Field(default="", description="Optional node id.")
    depth: int = Field(default=2, description="Optional traversal depth.")
    target: str = Field(default="", description="Optional target.")


class GraphExplainRequest(BaseModel):
    """Request body for ``POST /graph/explain`` — REST twin of the
    ``graph_explain`` MCP tool (the universal cited-context plane). Same
    factory/strictness proof as :class:`GraphCodeRequest` (``extra="forbid"``).
    """

    model_config = ConfigDict(extra="forbid")

    action: GraphExplainAction = Field(
        default="explain",
        description="'explain' (one cited answer), 'context' (synthesized "
        "context bundle), 'executable_rag' (grounded multi-hop retrieval), "
        "or 'recommend' (ranked items, not a single answer).",
    )
    query: str = Field(
        default="", description="The question, or intent/history for 'recommend'."
    )
    top_k: int = Field(default=10, description="Result count.")
    node_id: str = Field(default="", description="Optional anchor node id.")
    depth: int = Field(default=2, description="Optional traversal depth.")
    target: str = Field(
        default="",
        description="'domain:intent' (e.g. 'ops:why', 'code:usage'), a bare "
        "intent with the domain inferred, or 'domains' to list providers.",
    )


# ══════════════════════════════════════════════════════════════════
# 5. graph_configure (generic + the three bespoke granular twins this lane
#    owns: doctor, secret, vault-sync)
# ══════════════════════════════════════════════════════════════════


class GraphConfigureRequest(BaseModel):
    """Request body for ``POST /graph/configure`` — REST twin of the
    ``graph_configure`` MCP tool (``arbora/mcp_server/backing/tools/analysis_tools/py``).

    ``extra="forbid"``: PROVEN — ``graph_configure_endpoint`` blind-splats
    ``**body`` into ``_execute_tool("graph_configure", **body)``, and the
    tool's signature is exactly ``action``/``config_key``/``config_value``
    with no ``**kwargs``, so an unrecognized field is rejected deep inside
    `_execute_tool` the same way as `graph_orchestrate` above.

    ⚠ SECRET HANDLING: `config_value` is the generic payload/secret carrier
    for actions like `set_secret`/`vault_sync` — see the bespoke
    :class:`GraphConfigureSecretRequest`/:class:`GraphConfigureVaultSyncRequest`
    models below for the routes that exist specifically to carry secret
    material, and for confirmation that the RESPONSE never echoes it back.
    This generic model is documented as a general configuration surface, not
    a secrets endpoint, and should not be used to build examples containing
    real credentials.
    """

    model_config = ConfigDict(extra="forbid")

    action: str = Field(
        default="register_mcp",
        description="Configuration operation. Not a closed enum in the tool "
        "itself (plain `str`, not `Literal`) — known values include "
        "set_secret, vault_sync, register_mcp, install_hooks, "
        "uninstall_hooks, doctor (note: handled but NOT listed in the "
        "tool's own description string — a minor doc gap found while "
        "deriving this model), config_doctor, system_doctor, health, "
        "preflight, generate_config, get_config, set_config, list_config, "
        "add_connection, remove_connection, list_connections, "
        "discover_connection_schema, and the Stardog "
        "push_to_stardog/pull_from_stardog/stardog_sparql/"
        "stardog_export_graph/stardog_import_graph actions.",
        json_schema_extra={"examples": ["doctor", "list_connections"]},
    )
    config_key: str = Field(
        default="",
        description="The key/ID of the configuration or secret being acted "
        "on (connection name, schema-pack name, etc. — action-dependent).",
    )
    config_value: str = Field(
        default="",
        description="JSON-encoded string payload for the action — MAY carry "
        "secret material for `set_secret`/`vault_sync` (see the bespoke "
        "models below); never logged or echoed by this tool's own success "
        "responses.",
    )


class GraphConfigureResponse(BaseModel):
    """Response envelope for ``POST /graph/configure``.

    The tool returns ``str`` (JSON-encoded); `result` shape varies per
    `action` (the tool has 20+ branches with different return shapes) —
    modeled permissively rather than inventing one structure.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    result: dict[str, Any] | str = Field(
        description="Action-dependent payload; free-form dict for nearly "
        "every action, occasionally a bare string."
    )


class GraphConfigureSecretRequest(BaseModel):
    """Request body for ``POST /graph/configure/secret`` — REST twin of
    ``graph_configure(action='set_secret')`` (``graph_configure_secret_endpoint``
    in ``runtime.py``).

    ``extra="allow"``: PROVEN — the handler reads exactly `config_key` and
    `config_value` via `body.get(...)` and forwards only those two as fixed
    kwargs; any other body field is silently ignored, never rejected.

    ⚠ SECRETS: `config_value` IS the raw secret value being written. This
    field is intentionally undocumented with any example value, and no
    field on this model or :class:`GraphConfigureSecretResponse` echoes it —
    confirmed by reading the handler chain down to
    ``create_secrets_client().set(config_key, config_value)``, whose success
    path returns only ``{"status": "success", "action": "set_secret",
    "stored": true}``.
    """

    model_config = ConfigDict(extra="allow")

    config_key: str = Field(
        default="",
        description="The secret's key/name (e.g. an env var name, or "
        "'xai/...' to route to the xAI-scoped secrets client). NOT a secret "
        "value itself.",
    )
    config_value: str = Field(
        default="",
        description="THE SECRET VALUE to store. Write-only: never returned "
        "in any response. Do not populate this field with example data in "
        "documentation or client generators.",
    )


class SetSecretResult(BaseModel):
    """The exact, verified `result` payload of a successful `set_secret` —
    deliberately minimal so nothing beyond this shape is ever assumed safe
    to surface for a secrets-write endpoint.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    action: Literal["set_secret"] = Field(description="Echoes the action performed.")
    stored: bool = Field(description="Whether the secret was written. Never the value.")


class GraphConfigureSecretResponse(BaseModel):
    """Response envelope for ``POST /graph/configure/secret``. See
    :class:`SetSecretResult` — this NEVER carries the secret value.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    result: SetSecretResult = Field(
        description="Confirmation only — no secret material."
    )


class GraphConfigureVaultSyncRequest(BaseModel):
    """Request body for ``POST /graph/configure/vault-sync`` — REST twin of
    ``graph_configure(action='vault_sync')`` (``graph_configure_vault_sync_endpoint``
    in ``runtime.py``, CONCEPT:AU-OS.deployment.vault-first-routine-genesis).

    ``extra="allow"``: PROVEN the same way as :class:`GraphConfigureSecretRequest`
    — only `config_key`/`config_value` are read.

    ⚠ SECRETS: `config_value` is a JSON-encoded string of
    ``{"env_keys": [...], "values": {KEY: VALUE}, "overwrite": bool}`` —
    `values` MAY carry raw secret material being seeded. Confirmed safe on
    the way out: :class:`VaultSyncResult` below only ever contains
    ``vault://<service>/<KEY>`` REFERENCES and key NAMES (`present`/
    `written`/`missing`), never the values themselves (verified against
    ``SecretsClient.vault_sync``'s docstring and return statement).
    """

    model_config = ConfigDict(extra="allow")

    config_key: str = Field(
        default="",
        description=(
            "Logical service name (the `apps/<service>` secrets-store path segment)."
        ),
    )
    config_value: str = Field(
        default="",
        description='JSON-encoded `{"env_keys": [...], "values": {KEY: VALUE}, '
        '"overwrite": bool}`. `values` MAY carry raw secret material being '
        "seeded — write-only, never echoed back (see `VaultSyncResult`).",
        json_schema_extra={
            "examples": ['{"env_keys": ["API_TOKEN"], "overwrite": false}']
        },
    )


class VaultSyncResult(BaseModel):
    """The exact, verified `result` payload of a successful `vault_sync` —
    only vault:// reference URIs and key NAMES, never secret values (see
    :class:`GraphConfigureVaultSyncRequest` docstring for how this was
    confirmed).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    action: Literal["vault_sync"] = Field(description="Echoes the action performed.")
    service: str = Field(description="The service name this sync ran against.")
    refs: dict[str, str] = Field(
        description="`{KEY: 'vault://<service>/<KEY>'}` for every env key — "
        "resolvable references, never the underlying values."
    )
    present: list[str] = Field(
        description=(
            "Env key names that already existed in the store and were kept as-is."
        )
    )
    written: list[str] = Field(
        description="Env key names that were just written by this call."
    )
    missing: list[str] = Field(
        description="Env key names with neither a stored value nor a supplied one."
    )


class GraphConfigureVaultSyncResponse(BaseModel):
    """Response envelope for ``POST /graph/configure/vault-sync``. See
    :class:`VaultSyncResult` — references and key names only, never values.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    result: VaultSyncResult = Field(
        description="References + key names only — no secret material."
    )


class HookDoctorEntry(BaseModel):
    """One agent surface's hook-installation health, as returned by
    ``HookInstaller.doctor()`` (``agent_utilities/ecosystem/hook_installer.py``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(description="Human-readable agent-surface name.")
    path: str | None = Field(
        description="Filesystem path to that surface's hook config, or null "
        "when the surface has no on-disk config (e.g. an integrated surface)."
    )
    status: Literal["healthy", "stale", "not_installed", "integrated", "n/a"] = Field(
        description="'healthy' (contains 'agent-utilities'), 'stale' (config "
        "exists but doesn't reference agent-utilities), 'not_installed' (no "
        "file yet), 'integrated' (agent-terminal-ui, no separate config "
        "file), or 'n/a'."
    )
    size_bytes: int | None = Field(
        default=None,
        description="Config file size in bytes; present only when `path` "
        "exists on disk.",
    )


class GraphConfigureDoctorResponse(BaseModel):
    """Response envelope for ``POST /graph/configure/doctor`` — REST twin of
    ``graph_configure(action='doctor')`` (``graph_configure_doctor_endpoint``
    in ``runtime.py``, no request body). `result` is keyed by internal
    agent-surface identifiers (e.g. 'claude-code', 'agent-terminal-ui') —
    modeled as an open mapping rather than a closed key set since the agent
    surface roster can grow.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    result: dict[str, HookDoctorEntry] = Field(
        description="Agent-surface id -> hook health entry."
    )


# ══════════════════════════════════════════════════════════════════
# 7. /goals (GET+POST), /goals/{goal_id}/cancel, /goals/{goal_id}/iterations
# ══════════════════════════════════════════════════════════════════


class GoalRecord(BaseModel):
    """One autonomous goal-loop record, as returned by both `GET /goals`
    (a flat list of these) and `GET /goals/{goal_id}/iterations` (one of
    these, or a 404 error body — not modeled here). Sourced from
    ``agent_utilities/core/sessions.py``'s in-memory `active_goals` entries
    AND the KG-Loop-node fallback (`_goal_row_to_entry`) — the two shapes
    are IDENTICAL except `created_at`, which the KG-derived fallback never
    populates (hence optional here, not required).

    ``extra="allow"``: the in-memory and KG-derived producers are
    independently maintained dicts; nothing in the read path enforces a
    closed key set, so a future field addition to either producer should
    not be silently dropped by this model.
    """

    model_config = ConfigDict(extra="allow")

    goal_id: str = Field(description="The goal's unique id.")
    session_id: str = Field(description="The durable session this goal is attached to.")
    status: str = Field(
        description="Lifecycle status, e.g. 'submitted', 'queued', 'running', "
        "'succeeded', 'failed', 'cancelled', 'ready'."
    )
    objective: str = Field(description="The natural-language goal objective.")
    owner_host: str = Field(
        default="",
        description="The dispatch worker host currently (or last) owning this goal.",
    )
    created_at: float | None = Field(
        default=None,
        description="Unix timestamp the goal was created. Populated for "
        "goals still tracked in-memory on the host that created them; "
        "absent (null) for a goal rehydrated from its KG Loop node only.",
    )
    iterations: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Recorded iteration steps for this goal run (free-form "
        "per-iteration shape).",
    )
    total_iterations: int = Field(
        default=0, description="Count of iterations run so far."
    )
    total_duration_ms: int = Field(
        default=0,
        description="Cumulative wall-clock duration across iterations, in ms.",
    )
    total_tool_calls: int = Field(
        default=0, description="Cumulative tool-call count across iterations."
    )
    summary: str = Field(
        default="", description="Latest human-readable status summary."
    )
    error: str = Field(
        default="", description="Error detail when `status` is 'failed'."
    )


class CreateGoalRequest(BaseModel):
    """Request body for ``POST /goals`` — launches a new backgrounded
    autonomous goal-loop (``create_goal`` in ``agent_utilities/core/sessions.py``).

    ``extra="allow"``: PROVEN — the handler reads exactly `objective`,
    `max_iterations`, `validation_cmd`, and `constraints` via `body.get(...)`;
    any other field is silently ignored.
    """

    model_config = ConfigDict(extra="allow")

    objective: str = Field(
        description="The goal to pursue, in natural language. REQUIRED — an "
        "empty/missing value returns 400.",
        json_schema_extra={"examples": ["Get the failing suite green again"]},
    )
    max_iterations: int | None = Field(
        default=None,
        description="Cap on loop iterations. Omitted/falsy keeps the "
        "GoalSpec's own default.",
    )
    validation_cmd: str | None = Field(
        default=None,
        description="Shell command the loop runs to check whether the "
        "objective is satisfied.",
    )
    constraints: list[Any] | None = Field(
        default=None,
        description="Optional list of constraint entries carried through to "
        "the GoalSpec, sanitized before storage.",
    )


class CreateGoalResponse(BaseModel):
    """Response body for a successful ``POST /goals``. NOT wrapped in a
    `{"status", "result"}` envelope — the handler returns this shape directly.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    goal_id: str = Field(description="The newly created goal's id.")
    session_id: str = Field(description="The newly created durable session's id.")
    objective: str = Field(description="The parsed objective (echoed back).")
    validation_cmd: str = Field(
        description="The parsed validation command, empty string if none was supplied."
    )
    dispatch: dict[str, Any] = Field(
        description="The queue-dispatch handle from `enqueue_agent_turn` — "
        "free-form, dispatch-backend-dependent."
    )


class ListGoalsResponse(BaseModel):
    """Wrapper purely for documentation/testing purposes — ``GET /goals``
    itself returns a bare JSON array, not an object. Use `.root` conceptually
    as `list[GoalRecord]`; prefer constructing/validating
    `list[GoalRecord]` directly against the route's real body.
    """

    model_config = ConfigDict(extra="forbid")

    goals: list[GoalRecord] = Field(
        description="Every active + durable goal, merged (KG-derived entries "
        "first, then in-memory entries override by id) — NOT the route's "
        "literal JSON shape (which is the bare array); this field exists so "
        "the array's item type has one documented, importable name."
    )


class CancelGoalResponse(BaseModel):
    """Response body for ``POST /goals/{goal_id}/cancel``
    (``cancel_goal`` in ``agent_utilities/core/sessions.py``). A goal-not-found
    404 returns `{"error": "Active goal run not found"}` instead — not
    modeled here (error shape, not the success contract).
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = Field(description="Always 'success' on this path.")
    message: str = Field(description="Human-readable confirmation.")


# ══════════════════════════════════════════════════════════════════
# 8. /connector/run, /connector/sources
# ══════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════
# 9. /sessions
# ══════════════════════════════════════════════════════════════════


class SessionRecord(BaseModel):
    """One durable agent-session row, as returned in the flat list from
    ``GET /sessions`` (``get_all_sessions`` in ``agent_utilities/core/sessions.py``).
    Field set matches the `sessions` table schema exactly (see
    `_connect_db`'s `CREATE TABLE` statement in that module) plus the two
    boolean coercions the handler applies (`background`/`needs_input` are
    stored as 0/1 and converted to real booleans before the response is built).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="The session's unique id.")
    title: str = Field(default="", description="Display title.")
    created_at: float = Field(description="Unix timestamp the session was created.")
    updated_at: float = Field(
        description="Unix timestamp of the session's last update."
    )
    model: str = Field(default="", description="Model id used for this session.")
    mode: str = Field(default="ask", description="Session mode, e.g. 'ask'.")
    workspace: str = Field(
        default="", description="Workspace path the session is scoped to."
    )
    turn_count: int = Field(default=0, description="Number of turns recorded so far.")
    status: str = Field(
        default="active",
        description="Lifecycle status, e.g. 'active', 'queued', 'cancelled'.",
    )
    background: bool = Field(
        description="Whether this session runs a backgrounded goal loop "
        "(coerced from the stored 0/1 integer)."
    )
    needs_input: bool = Field(
        description="Whether the session is blocked waiting on user input "
        "(coerced from the stored 0/1 integer)."
    )
    last_response_preview: str = Field(
        default="", description="Truncated preview of the most recent response."
    )
    goal_id: str = Field(
        default="",
        description="The attached goal's id, empty when this session has none.",
    )
    metadata_json: str = Field(
        default="{}", description="JSON-encoded string of additional session metadata."
    )
    tenant_id: str = Field(
        default="", description="Owning tenant id (AU-P0-5 row-level scoping column)."
    )


class SessionsListQuery(BaseModel):
    """Query parameters for ``GET /sessions``. The handler clamps both
    values server-side rather than rejecting an out-of-range value, so this
    model mirrors that behavior in its field descriptions instead of adding
    a stricter validator that would reject something the real endpoint
    happily accepts and clamps.
    """

    model_config = ConfigDict(extra="allow")

    limit: int = Field(
        default=500,
        description="Max rows to return. Server-clamped to [1, 2000] — an "
        "out-of-range value is not rejected, just clamped.",
    )
    offset: int = Field(
        default=0,
        description="Row offset for pagination. Server-clamped to >= 0.",
    )
