"""Graph OS configuration reads and the platform configuration location.

Every Graph OS module reads a setting through :func:`setting` (the environment
at call time, typed by the default) and locates its configuration files through
:func:`config_dir` / :func:`platform_config_file`. Graph OS owns
``~/.config/graph-os``: ``GRAPH_OS_CONFIG_DIR`` wins, then
``$XDG_CONFIG_HOME/graph-os``. For one release a file that exists only under the
legacy ``AGENT_UTILITIES_CONFIG_DIR`` (or ``$XDG_CONFIG_HOME/agent-utilities``)
location is still found, with a single deprecation warning.
"""

from __future__ import annotations

import json
import os
import threading
import warnings
from functools import cache
from pathlib import Path
from typing import Any, Literal

from agent_connector_sdk.config import csv_values, setting
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

__all__ = [
    "CONFIG_DIR_SETTING",
    "GraphOSConfig",
    "config_dir",
    "csv_values",
    "plaintext_secret_keys",
    "write_private_mapping",
    "is_production_profile",
    "load_config",
    "platform_config_file",
    "runtime_config",
    "save_config_item",
    "setting",
]

CONFIG_DIR_SETTING = "GRAPH_OS_CONFIG_DIR"
_LEGACY_CONFIG_DIR_SETTING = "AGENT_UTILITIES_CONFIG_DIR"
_APP_DIR = "graph-os"
_LEGACY_APP_DIR = "agent-utilities"
_warned_legacy: set[Path] = set()


def _config_home() -> Path:
    return Path(setting("XDG_CONFIG_HOME", "") or Path.home() / ".config")


def _env_dir(name: str) -> Path | None:
    raw = str(setting(name, "") or "").strip()
    return Path(raw).expanduser() if raw else None


def config_dir() -> Path:
    """``GRAPH_OS_CONFIG_DIR`` or ``$XDG_CONFIG_HOME/graph-os`` (not created)."""
    return _env_dir(CONFIG_DIR_SETTING) or _config_home() / _APP_DIR


def _legacy_config_dir() -> Path:
    return _env_dir(_LEGACY_CONFIG_DIR_SETTING) or _config_home() / _LEGACY_APP_DIR


def platform_config_file(name: str) -> Path:
    """Return the path of configuration file ``name``.

    The Graph OS location is returned unless the file is absent there, no
    explicit ``GRAPH_OS_CONFIG_DIR`` is set, and it exists at the legacy
    location; that fallback warns once per legacy directory.
    """
    preferred = config_dir() / name
    if preferred.exists() or _env_dir(CONFIG_DIR_SETTING) is not None:
        return preferred
    legacy_dir = _legacy_config_dir()
    legacy = legacy_dir / name
    if not legacy.exists():
        return preferred
    if legacy_dir not in _warned_legacy:
        _warned_legacy.add(legacy_dir)
        warnings.warn(
            f"reading Graph OS configuration from legacy {legacy_dir}; "
            f"move it to {config_dir()}",
            DeprecationWarning,
            stacklevel=2,
        )
    return legacy


_CREDENTIAL_SUFFIXES = ("_SECRET", "_PASSWORD", "_TOKEN", "_API_KEY", "_PRIVATE_KEY")
_REFERENCE_SCHEMES = ("env://", "openbao://", "vault://", "file://")
_CONFIG_FILE = "config.json"
_write_lock = threading.Lock()


class GraphOSConfig(BaseModel):
    """The typed Graph OS settings, read from the environment by alias.

    ``GraphOSConfig()`` takes one snapshot of the environment. Use
    :data:`runtime_config` for a live read of one field at call time.
    """

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    agent_dispatch_claim_ttl_s: float = Field(
        default=120.0,
        alias="AGENT_DISPATCH_CLAIM_TTL_S",
        description="Renewable dispatch-claim lifetime in seconds. The upper bound is the workload contract's recovery-time objective: after a worker dies, another worker can reclaim its turn within five minutes without an operator-specific override.",
    )
    agent_dispatch_renew_interval_s: float = Field(
        default=30.0,
        alias="AGENT_DISPATCH_RENEW_INTERVAL_S",
        description="Maximum interval between dispatch lease renewals. AgentConfig's bounds make this strictly shorter than every valid dispatch claim TTL. Tests that inject a smaller lease use one third of that injected TTL so the same invariant still holds.",
    )
    agent_policies_path: str | None = Field(
        default=None,
        alias="AGENT_POLICIES_PATH",
        description="Path to agent_policies.json for identity-based governance (CONCEPT:AU-OS.state.cognitive-scheduler-preemption).",
    )
    agent_utilities_config_dir: str | None = Field(
        default=None, alias="AGENT_UTILITIES_CONFIG_DIR", description=""
    )
    allowed_hosts: str | None = Field(
        default=None,
        alias="ALLOWED_HOSTS",
        description="Comma-separated Host-header allowlist for ``TrustedHostMiddleware``. Non-loopback listeners must configure this explicitly.",
    )
    allowed_origins: str | None = Field(
        default=None,
        alias="ALLOWED_ORIGINS",
        description="Comma-separated list of allowed CORS origins. Unset disables CORS.",
    )
    app_profile: str = Field(
        default="dev",
        alias="APP_PROFILE",
        description="Deployment posture forwarded to locally autostarted components.",
    )
    auth_jwt_audience: str | None = Field(
        default=None,
        alias="AUTH_JWT_AUDIENCE",
        description="Expected JWT audience claim for validation.",
    )
    auth_jwt_issuer: str | None = Field(
        default=None,
        alias="AUTH_JWT_ISSUER",
        description="Expected JWT issuer claim for validation.",
    )
    auth_jwt_jwks_uri: str | None = Field(
        default=None,
        alias="AUTH_JWT_JWKS_URI",
        description="JWKS URI for JWT Bearer token verification (e.g. Azure AD, Okta).",
    )
    cert_artifacts_dir: str | None = Field(
        default=None, alias="CERT_ARTIFACTS_DIR", description=""
    )
    cert_evidence_signer_command: list[str] = Field(
        default_factory=lambda: [], alias="CERT_EVIDENCE_SIGNER_COMMAND", description=""
    )
    cert_evidence_verifier_command: list[str] = Field(
        default_factory=lambda: [],
        alias="CERT_EVIDENCE_VERIFIER_COMMAND",
        description="",
    )
    cert_fault_action_commands: Any = Field(
        default_factory=lambda: {}, alias="CERT_FAULT_ACTION_COMMANDS", description=""
    )
    cert_fault_probe_commands: Any = Field(
        default_factory=lambda: {}, alias="CERT_FAULT_PROBE_COMMANDS", description=""
    )
    cert_hardware_class: str | None = Field(
        default=None, alias="CERT_HARDWARE_CLASS", description=""
    )
    cert_hook_commands: Any = Field(
        default_factory=lambda: {}, alias="CERT_HOOK_COMMANDS", description=""
    )
    cert_load_command: list[str] = Field(
        default_factory=lambda: [], alias="CERT_LOAD_COMMAND", description=""
    )
    cert_metrics_command: list[str] = Field(
        default_factory=lambda: [], alias="CERT_METRICS_COMMAND", description=""
    )
    cert_prometheus_bearer_token_ref: str | None = Field(
        default=None, alias="CERT_PROMETHEUS_BEARER_TOKEN_REF", description=""
    )
    cert_prometheus_tls_profile: str | None = Field(
        default=None, alias="CERT_PROMETHEUS_TLS_PROFILE", description=""
    )
    cert_prometheus_tls_profile_ref: str | None = Field(
        default=None, alias="CERT_PROMETHEUS_TLS_PROFILE_REF", description=""
    )
    cert_prometheus_url: str | None = Field(
        default=None, alias="CERT_PROMETHEUS_URL", description=""
    )
    cert_release_manifest: str | None = Field(
        default=None, alias="CERT_RELEASE_MANIFEST", description=""
    )
    certification_mode: Literal["disabled", "production"] = Field(
        default="disabled", alias="CERTIFICATION_MODE", description=""
    )
    chat_models: Any = Field(
        default_factory=lambda: [], alias="CHAT_MODELS", description=""
    )
    comfyui_url: str | None = Field(default=None, alias="COMFYUI_URL", description="")
    cors_allow_credentials: bool = Field(
        default=False,
        alias="CORS_ALLOW_CREDENTIALS",
        description="Allow browser credentials for explicitly enumerated CORS origins. Wildcard origins are rejected when this option is enabled.",
    )
    data_prep_runtime: Any = Field(
        default=None,
        alias="DATA_PREP_RUNTIME",
        description="Declarative owner configuration for the governed data-prep runtime. The startup loader accepts only model field/type declarations, policy facts and the fixed native ICV/shape profile. It rejects import, executable, storage and authority controls; tool callers never supply it.",
    )
    debug: bool = Field(default=False, alias="DEBUG", description="")
    delegated_scopes: str = Field(
        default="api",
        alias="DELEGATED_SCOPES",
        description="Space-separated scopes requested during token delegation.",
    )
    delegation_audience: str | None = Field(
        default=None,
        alias="AUDIENCE",
        description="Target audience for delegated tokens (e.g. the downstream API base URL).",
    )
    deployment_profile: Literal["tiny", "single-node-prod", "enterprise"] = Field(
        default="tiny",
        alias="DEPLOYMENT_PROFILE",
        description="Explicit Agent Utilities deployment shape; independent of APP_PROFILE.",
    )
    embedding_models: Any = Field(
        default_factory=lambda: [], alias="EMBEDDING_MODELS", description=""
    )
    embedding_tls_profile: str | None = Field(
        default=None, alias="EMBEDDING_TLS_PROFILE", description=""
    )
    embedding_tls_profile_ref: str | None = Field(
        default=None, alias="EMBEDDING_TLS_PROFILE_REF", description=""
    )
    enable_delegation: bool = Field(
        default=False,
        alias="ENABLE_DELEGATION",
        description="Enable OIDC token delegation (RFC 8693 Token Exchange) for downstream API calls.",
    )
    enable_otel: bool = Field(default=False, alias="ENABLE_OTEL", description="")
    enable_web_ui: bool = Field(default=False, alias="ENABLE_WEB_UI", description="")
    engine_tls_profile: str | None = Field(
        default=None, alias="ENGINE_TLS_PROFILE", description=""
    )
    engine_tls_profile_ref: str | None = Field(
        default=None, alias="ENGINE_TLS_PROFILE_REF", description=""
    )
    eunomia_allowed_private_hosts: list[str] = Field(
        default_factory=lambda: [],
        alias="EUNOMIA_ALLOWED_PRIVATE_HOSTS",
        description="",
    )
    eunomia_api_key_ref: str | None = Field(
        default=None, alias="EUNOMIA_API_KEY_REF", description=""
    )
    eunomia_bulk_check_max: int = Field(
        default=100, alias="EUNOMIA_BULK_CHECK_MAX", description=""
    )
    eunomia_max_response_bytes: int = Field(
        default=1048576, alias="EUNOMIA_MAX_RESPONSE_BYTES", description=""
    )
    eunomia_policy_file: str | None = Field(
        default=None, alias="EUNOMIA_POLICY_FILE", description=""
    )
    eunomia_remote_url: str | None = Field(
        default=None, alias="EUNOMIA_REMOTE_URL", description=""
    )
    eunomia_timeout_seconds: float = Field(
        default=10.0, alias="EUNOMIA_TIMEOUT_SECONDS", description=""
    )
    eunomia_tls_profile: str | None = Field(
        default=None, alias="EUNOMIA_TLS_PROFILE", description=""
    )
    eunomia_tls_profile_ref: str | None = Field(
        default=None, alias="EUNOMIA_TLS_PROFILE_REF", description=""
    )
    eunomia_type: Literal["none", "embedded", "remote"] = Field(
        default="none", alias="EUNOMIA_TYPE", description=""
    )
    evolution_staging_root: str | None = Field(
        default=None,
        alias="EVOLUTION_STAGING_ROOT",
        description="Runtime-only root for reviewable evolution artifacts. The value is never emitted by doctor, logs, graph metadata, traces, or public API responses.",
    )
    external_graph_connectors: Any = Field(
        default_factory=lambda: [], alias="EXTERNAL_GRAPH_CONNECTORS", description=""
    )
    extra_headers: dict[str, str] | None = Field(
        default=None, alias="EXTRA_HEADERS", description=""
    )
    faster_whisper_url: str | None = Field(
        default=None, alias="FASTER_WHISPER_URL", description=""
    )
    fleet_events_token_ref: str | None = Field(
        default=None,
        alias="FLEET_EVENTS_TOKEN_REF",
        description="Secret-provider reference for the fleet-events webhook credential.",
    )
    fleet_mcp_url_template: str | None = Field(
        default=None,
        alias="FLEET_MCP_URL_TEMPLATE",
        description="Optional runtime-only MCP URL template containing ``{server}``. It supports both per-host and path-routed fleets without assuming a domain; endpoint identities are never included in durable reports or traces.",
    )
    flux_url: str | None = Field(default=None, alias="FLUX_URL", description="")
    frontend_contribution_trusted_signers: list[str] = Field(
        default_factory=lambda: [],
        alias="FRONTEND_CONTRIBUTION_TRUSTED_SIGNERS",
        description="Bounded signer-key-id allowlist for installed frontend contributions. Empty fails every contribution closed. REST and MCP both consume this one process-wide authority.",
    )
    gateway_metrics: bool = Field(
        default=True,
        alias="GATEWAY_METRICS",
        description="Expose Python-tier Prometheus metrics on the gateway: a pure-ASGI middleware recording ``agent_utilities_gateway_*`` series (request totals, duration histogram, in-flight gauge, rate-limit/breaker counters) plus a ``GET /metrics`` endpoint (exempt from auth — scrapers cannot mint JWTs). Requires...",
    )
    gateway_rate_burst: float = Field(
        default=0.0,
        alias="GATEWAY_RATE_BURST",
        description="Token-bucket burst capacity (max requests served instantly from a full bucket). ``0`` (default) derives 2× ``GATEWAY_RATE_LIMIT``.",
    )
    gateway_rate_limit: float = Field(
        default=0.0,
        alias="GATEWAY_RATE_LIMIT",
        description="Per-tenant sustained request rate (requests/second) enforced by the gateway token-bucket middleware. ``0`` (default) disables rate limiting. Bucket key: ActorContext tenant → authenticated actor id → client IP. Buckets are in-memory and PER-PROCESS: with N workers/replicas the effective limit is...",
    )
    google_workspace_oauth_broker_url: str | None = Field(
        default=None, alias="GOOGLE_WORKSPACE_OAUTH_BROKER_URL", description=""
    )
    google_workspace_oauth_client_id: str | None = Field(
        default=None, alias="GOOGLE_WORKSPACE_OAUTH_CLIENT_ID", description=""
    )
    graph_service_endpoints: list[str] | None = Field(
        default=None,
        alias="GRAPH_SERVICE_ENDPOINTS",
        description="Explicit engine coordinator contacts (comma-separated or JSON list). Any configured value is a connect-only topology: clients never start a local stand-in, even when an endpoint uses loopback or a Unix socket. Unset selects the packaged local engine lifecycle. Multiple entries are...",
    )
    host: str = Field(default="127.0.0.1", alias="HOST", description="")
    hunyuan_url: str | None = Field(default=None, alias="HUNYUAN_URL", description="")
    identity_group_capability_map: Any = Field(
        default=None,
        alias="IDENTITY_GROUP_CAPABILITY_MAP",
        description='Optional IdP group/role → capability mapping for provider-agnostic role inheritance (CONCEPT:AU-OS.identity.idp-agnostic-role-inheritance). A JSON object, e.g. ``{"okta-group-id-0oa...": ["kg-admin"], "engineering": ["kg-write"]}``. Lets an opaque Okta group id (or a differently-named...',
    )
    infra_inventory_path: str | None = Field(
        default=None, alias="INFRA_INVENTORY_PATH", description=""
    )
    kafka_bootstrap_servers: str = Field(
        default="",
        alias="KAFKA_BOOTSTRAP_SERVERS",
        description="Runtime-injected Kafka broker list (service DNS + raw TCP port). Every Kafka-consuming code path resolves through this field (``kafka_queue_backend.py``, ``bus_log.KafkaBusLog``, ``agent_dispatch.py``, ``ingest_worker.py``) rather than a hardcoded deployment hostname. HTTP ingress cannot proxy...",
    )
    kg_auth_token_ref: str | None = Field(
        default=None,
        alias="KG_AUTH_TOKEN_REF",
        description="Runtime secret reference resolving to the graph process JWT. It scopes engine bootstrap and stdio tool calls and is mutually exclusive with ``KG_IDENTITY_OAUTH2``. Raw tokens are never valid AgentConfig material.",
    )
    kg_connections: Any = Field(
        default=None,
        alias="KG_CONNECTIONS",
        description='Declarative named graph connections (CONCEPT:AU-KG.backend.multi-connection-registry). A JSON list of backend specs, each ``{"name": <str>, "backend": <type>, ...}``. Any endpoint, database, identity, credential, TLS, or local-path field must be a runtime secret reference. These are...',
    )
    kg_engine_tool_pool_size: int = Field(
        default=16,
        alias="KG_ENGINE_TOOL_POOL_SIZE",
        description="Max warm raw ``SyncEpistemicGraphClient`` wire connections kept resident by the low-level ``engine_<domain>`` MCP tools (AU-P0-6, ``mcp/tools/engine_tools.py``). Distinct from :attr:`kg_engine_pool_size` (which bounds the resident *compute-engine* pool, a different resource): this one bounds the...",
    )
    kg_failure_evolution: bool = Field(
        default=False, alias="KG_FAILURE_EVOLUTION", description=""
    )
    kg_fuseki_endpoint: str = Field(
        default="",
        alias="KG_FUSEKI_ENDPOINT",
        description="THE canonical Fuseki endpoint (CONCEPT:AU-KG.ontology.authoritative-tbox) — the single field every Fuseki reader resolves through: the ontology-publish tick (``engine_tasks._tick_fuseki_publish``) and ``publish_ontology_to_fuseki``'s endpoint fallback. (The ``jena_fuseki`` query backend and its...",
    )
    kg_identity_oauth2: Any = Field(
        default=None,
        alias="KG_IDENTITY_OAUTH2",
        description="OAuth2 client-credentials block used to acquire a short-lived graph process JWT. Client secrets must be runtime secret references.",
    )
    kg_loop_allow_host_validation: bool = Field(
        default=False, alias="KG_LOOP_ALLOW_HOST_VALIDATION", description=""
    )
    kg_loop_mine_discovery: bool = Field(
        default=True, alias="KG_LOOP_MINE_DISCOVERY", description=""
    )
    kg_policy_version: str | None = Field(
        default=None,
        alias="KG_POLICY_VERSION",
        description="Required immutable authorization-policy revision stamped into every server-minted graph session.",
    )
    lakehouse_s3_endpoint: str = Field(
        default="",
        alias="LAKEHOUSE_S3_ENDPOINT",
        description="SeaweedFS S3-compatible gateway backing the Iceberg lakehouse's warehouse files (``services/lakehouse-seaweedfs``). No S3 STS: per-warehouse credential vending stays disabled (``sts-enabled: false``); S3 identity/credentials are vended from OpenBao ``apps/lakehouse-seaweedfs``...",
    )
    lakekeeper_catalog_uri: str = Field(
        default="",
        alias="LAKEKEEPER_CATALOG_URI",
        description="Lakekeeper Iceberg REST catalog base URI (``services/lakekeeper``). Known gotcha: the deployed catalog is mounted at ``/catalog`` (i.e. requests land on ``/catalog/v1/...``), NOT bare ``/v1`` -- a URI missing the ``/catalog`` path segment 404s every catalog call.",
    )
    lakekeeper_db_uri_ref: str | None = Field(
        default=None,
        alias="LAKEKEEPER_DB_URI_REF",
        description="Runtime secret reference for the dedicated Lakekeeper Postgres DSN (``services/lakekeeper-db``); resolved only at the runtime boundary, never here.",
    )
    lakekeeper_oauth2_scope: str = Field(
        default="lakekeeper",
        alias="LAKEKEEPER_OAUTH2_SCOPE",
        description="OAuth2 scope requested from Keycloak (realm ``homelab``, ``https://<idp-host>/realms/<realm>``) when authenticating to the Lakekeeper catalog. Known gotcha: the Iceberg REST client's own default (``catalog``) is NOT granted to the deployed ``lakekeeper-service`` machine client -- only...",
    )
    lakekeeper_warehouse: str = Field(
        default="lakehouse",
        alias="LAKEKEEPER_WAREHOUSE",
        description="Iceberg warehouse name registered with the Lakekeeper catalog.",
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", alias="LANGFUSE_HOST", description=""
    )
    langfuse_kg_auto_ingest: bool = Field(
        default=False,
        alias="LANGFUSE_KG_AUTO_INGEST",
        description="Opt in to privacy-sanitized trace content; all sinks are metadata-only by default.",
    )
    langfuse_mcp_enabled: bool = Field(
        default=False,
        alias="LANGFUSE_MCP_ENABLED",
        description="Expose the Langfuse MCP integration when its runtime credentials are injected.",
    )
    langfuse_persistence_hmac_key_ref: str | None = Field(
        default=None, alias="LANGFUSE_PERSISTENCE_HMAC_KEY_REF", description=""
    )
    langfuse_public_key_ref: str | None = Field(
        default=None, alias="LANGFUSE_PUBLIC_KEY_REF", description=""
    )
    langfuse_secret_key_ref: str | None = Field(
        default=None, alias="LANGFUSE_SECRET_KEY_REF", description=""
    )
    langfuse_tls_profile: str | None = Field(
        default=None, alias="LANGFUSE_TLS_PROFILE", description=""
    )
    langfuse_tls_profile_ref: str | None = Field(
        default=None, alias="LANGFUSE_TLS_PROFILE_REF", description=""
    )
    max_upload_size: int = Field(
        default=10485760, alias="MAX_UPLOAD_SIZE", description=""
    )
    mcp_always_load: list[str] = Field(
        default_factory=lambda: [
            "tunnel-manager-mcp",
            "systems-manager-mcp",
            "repository-manager-mcp",
            "container-manager-mcp",
        ],
        alias="MCP_ALWAYS_LOAD",
        description="Fleet servers mounted EAGERLY on a session's first contact with graph-os, before any ``find_tools`` round trip (CONCEPT:AU-ECO.multiplexer.tool-gateway-catalog). Core operational capability should not cost a discovery hop. The motivating reason is concrete: semantic ``find_tools`` ranking...",
    )
    mcp_always_load_tools: list[str] = Field(
        default_factory=lambda: [
            "github-mcp:github_issues",
            "github-mcp:github_pulls",
            "gitlab-mcp:gitlab_issues",
            "gitlab-mcp:gitlab_merge_requests",
        ],
        alias="MCP_ALWAYS_LOAD_TOOLS",
        description="INDIVIDUAL fleet tools mounted eagerly, for servers too large to mount whole (CONCEPT:AU-ECO.multiplexer.tool-gateway-catalog). ``MCP_ALWAYS_LOAD`` exposes a server's entire condensed surface. Some servers are far too large for that — ``github-mcp`` and ``gitlab-mcp`` carry dozens of tools, and...",
    )
    mcp_basic_auth_password_ref: str | None = Field(
        default=None,
        alias="MCP_BASIC_AUTH_PASSWORD_REF",
        description="Runtime secret reference for outbound MCP HTTP Basic authentication.",
    )
    mcp_basic_auth_username: str | None = Field(
        default=None,
        alias="MCP_BASIC_AUTH_USERNAME",
        description="Runtime username for outbound MCP HTTP Basic authentication.",
    )
    mcp_bearer_token_file: str | None = Field(
        default=None,
        alias="MCP_BEARER_TOKEN_FILE",
        description="Local path to a mode-0600 file holding a bearer token an out-of-process daemon rotates (BUG-051, ``MCP_CLIENT_AUTH=rotating-file-bearer``). Read fresh on every outbound request; never cached across the file's lifetime.",
    )
    mcp_catalog_probe_ttl: float = Field(
        default=300.0,
        alias="MCP_CATALOG_PROBE_TTL",
        description="CONCEPT:AU-ECO.multiplexer.catalog-probe-ttl-staleness — fleet catalog-probe cache TTL. How long a cached fleet-probe result (``Multiplexer._probe_cache``) is served as fresh before it is reported ``stale`` and re-queued for a background re-probe. There is no correct universal default here — a...",
    )
    mcp_child_breaker_cooldown: float = Field(
        default=15.0,
        alias="MCP_CHILD_BREAKER_COOLDOWN",
        description="Seconds an open per-child circuit breaker waits before allowing a single half-open probe call; the probe's outcome closes or re-opens the circuit. Per-server override: ``breaker_cooldown``.",
    )
    mcp_child_breaker_threshold: int = Field(
        default=5,
        alias="MCP_CHILD_BREAKER_THRESHOLD",
        description="Consecutive transport failures/timeouts on one multiplexer child before its circuit breaker opens (fast, typed ``MCPChildCircuitOpenError`` instead of hammering a dead child). ``0`` disables the breaker. Per-server override: ``breaker_threshold``.",
    )
    mcp_child_max_concurrency: int = Field(
        default=8,
        alias="MCP_CHILD_MAX_CONCURRENCY",
        description="Maximum in-flight tool calls per multiplexer child server. Excess calls queue (bounded by ``MCP_CHILD_QUEUE_TIMEOUT``) instead of piling onto the child unbounded. Per-server override: the ``max_concurrency`` key on the server's ``mcp_config.json`` entry. The limit cannot be disabled.",
    )
    mcp_child_max_restarts: int = Field(
        default=5,
        alias="MCP_CHILD_MAX_RESTARTS",
        description="How many automatic restarts a crashed multiplexer child may consume within ``MCP_CHILD_RESTART_WINDOW`` before it is marked ``failed`` (calls then fail fast with the typed ``MCPChildUnavailableError`` instead of retry-looping forever). ``0`` disables auto-restart entirely.",
    )
    mcp_child_pool_size: int = Field(
        default=1,
        alias="MCP_CHILD_POOL_SIZE",
        description="Session-pool size for remote (streamable-http/SSE) multiplexer children: N independent connections per child, round-robin dispatched, enabling parallel in-flight calls. Default 1 preserves the historical one-connection resource profile. Stdio children are single-pipe and always keep exactly one...",
    )
    mcp_child_queue_timeout: float = Field(
        default=30.0,
        alias="MCP_CHILD_QUEUE_TIMEOUT",
        description="Seconds a tool call may wait for a free per-child concurrency slot before failing with the typed ``MCPChildBusyError`` (no silent hangs). Per-server override: the ``queue_timeout`` key on the server entry.",
    )
    mcp_child_restart_window: float = Field(
        default=300.0,
        alias="MCP_CHILD_RESTART_WINDOW",
        description="Sliding window (seconds) over which ``MCP_CHILD_MAX_RESTARTS`` is counted. Restarts older than the window are forgiven, so a child that crashes rarely keeps restarting indefinitely while a crash-looping child is parked as ``failed``.",
    )
    mcp_client_auth: Literal[
        "none", "oidc-client-credentials", "basic", "rotating-file-bearer"
    ] = Field(
        default="none",
        alias="MCP_CLIENT_AUTH",
        description="Outbound MCP child-auth mode. Enabled modes fail closed when incomplete. ``rotating-file-bearer`` (BUG-051) re-reads ``MCP_BEARER_TOKEN_FILE`` fresh on every outbound request instead of caching a bearer at connect time — for a token an out-of-process refresh daemon rotates on its own schedule.",
    )
    mcp_config: str | None = Field(default=None, alias="MCP_CONFIG", description="")
    mcp_dynamic_discovery_timeout: float = Field(
        default=5.0,
        alias="MCP_DYNAMIC_DISCOVERY_TIMEOUT",
        description="Wall-clock budget for an interactive fleet capability lookup. The fleet can contain hundreds of independently deployed MCP servers. A first-use discovery must return partial, explicitly-unavailable results rather than make an interactive GraphOS request wait for every endpoint's per-server probe...",
    )
    mcp_dynamic_top_k: int = Field(
        default=8,
        alias="MCP_DYNAMIC_TOP_K",
        description="Default number of ranked tool candidates ``find_tools`` returns when the caller does not specify ``top_k``. Kept small so the discovery result is itself cheap to read; callers can request more explicitly.",
    )
    mcp_fleet_secret_refs: dict[str, str] = Field(
        default_factory=lambda: {},
        alias="MCP_FLEET_SECRET_REFS",
        description="Neutral MCP fleet alias to runtime secret-reference mapping. Persistent child catalogs retain ``env://ALIAS`` values. At the child boundary, a directly projected alias wins; this mapping is consulted only when that alias is unavailable. Values must be ``env://``, ``vault://``, or ``secret://``...",
    )
    mcp_http_allowed_private_hosts: list[str] = Field(
        default_factory=lambda: [],
        alias="MCP_HTTP_ALLOWED_PRIVATE_HOSTS",
        description="Exact private hostnames permitted for DNS-pinned remote MCP children.",
    )
    mcp_jwt_audience: str | None = Field(
        default=None, alias="FASTMCP_SERVER_AUTH_JWT_AUDIENCE", description=""
    )
    mcp_jwt_issuer: str | None = Field(
        default=None, alias="FASTMCP_SERVER_AUTH_JWT_ISSUER", description=""
    )
    mcp_stdio_prohibited: bool = Field(
        default=False,
        alias="MCP_STDIO_PROHIBITED",
        description="Prohibit spawning stdio-transport MCP children in THIS process. Default permissive (False) so no other deployment's behavior changes. A stdio child is a subprocess spawned inside the calling process itself (not a network peer) -- a deployment that fans out across the whole MCP catalog (e.g....",
    )
    mcp_url: str | None = Field(default=None, alias="MCP_URL", description="")
    memento_raw_encryption_key_ref: str | None = Field(
        default=None,
        alias="MEMENTO_RAW_ENCRYPTION_KEY_REF",
        description="Secrets-backend reference for the Memento raw-retention encryption key.",
    )
    memento_raw_retention_enabled: bool = Field(
        default=False,
        alias="MEMENTO_RAW_RETENTION_ENABLED",
        description="Opt in to encrypted raw Memento retention; disabled by default.",
    )
    memento_raw_retention_policy: str = Field(
        default="",
        alias="MEMENTO_RAW_RETENTION_POLICY",
        description="Must equal the supported versioned approval token before raw retention is allowed.",
    )
    messaging_alert_intake_allow_remote: bool = Field(
        default=False, alias="MESSAGING_ALERT_INTAKE_ALLOW_REMOTE", description=""
    )
    messaging_alert_intake_host: str = Field(
        default="127.0.0.1", alias="MESSAGING_ALERT_INTAKE_HOST", description=""
    )
    messaging_alert_intake_port: int | None = Field(
        default=None, alias="MESSAGING_ALERT_INTAKE_PORT", description=""
    )
    messaging_alert_intake_token_ref: str | None = Field(
        default=None, alias="MESSAGING_ALERT_INTAKE_TOKEN_REF", description=""
    )
    messaging_intake_enabled: Any = Field(
        default=False,
        alias="MESSAGING_INTAKE_ENABLED",
        description="Explicit deployment intent for embedded inbound messaging intake. Credentials alone never grant polling ownership. Only the designated deployment request container may set this to ``true``; all other clients remain outbound-capable with the fail-closed default. Environment values are...",
    )
    model_http_allowed_private_hosts: list[str] = Field(
        default_factory=lambda: [],
        alias="MODEL_HTTP_ALLOWED_PRIVATE_HOSTS",
        description="Exact runtime-configured private/loopback hosts that model clients may reach through the DNS-pinned transport. Empty denies private model egress; public HTTPS providers remain available. Values are never emitted by doctor, logs, traces, graph records, or public APIs.",
    )
    model_registry_path: str | None = Field(
        default=None,
        alias="MODEL_REGISTRY_PATH",
        description="Path to a YAML or JSON file defining the model registry.",
    )
    model_tls_profile: str | None = Field(
        default=None, alias="MODEL_TLS_PROFILE", description=""
    )
    model_tls_profile_ref: str | None = Field(
        default=None, alias="MODEL_TLS_PROFILE_REF", description=""
    )
    oauth2_token_tls_profile: str | None = Field(
        default=None, alias="OAUTH2_TOKEN_TLS_PROFILE", description=""
    )
    oauth2_token_tls_profile_ref: str | None = Field(
        default=None, alias="OAUTH2_TOKEN_TLS_PROFILE_REF", description=""
    )
    oidc_audience: str | None = Field(
        default=None,
        alias="OIDC_AUDIENCE",
        description="Exact audience requested for outbound MCP child credentials.",
    )
    oidc_client_id: str | None = Field(
        default=None,
        alias="OIDC_CLIENT_ID",
        description="OAuth 2.0 client ID registered with the Identity Provider.",
    )
    oidc_client_secret_ref: str | None = Field(
        default=None,
        alias="OIDC_CLIENT_SECRET_REF",
        description="Runtime secret reference for the OAuth 2.0 client secret.",
    )
    oidc_config_url: str | None = Field(
        default=None,
        alias="OIDC_CONFIG_URL",
        description="OIDC discovery URL (e.g. https://idp.example.com/.well-known/openid-configuration). Works with any OIDC-compliant Identity Provider.",
    )
    oidc_issuer: str | None = Field(
        default=None,
        alias="OIDC_ISSUER",
        description="Explicit issuer used for provider-neutral token-endpoint discovery.",
    )
    oidc_token_url: str | None = Field(
        default=None,
        alias="OIDC_TOKEN_URL",
        description="Explicit token endpoint; when absent, discovery uses ``OIDC_ISSUER``.",
    )
    openai_tts_url: str | None = Field(
        default=None, alias="OPENAI_TTS_URL", description=""
    )
    otel_exporter_otlp_endpoint: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_ENDPOINT", description=""
    )
    otel_exporter_otlp_headers_ref: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_HEADERS_REF", description=""
    )
    otel_exporter_otlp_public_key_ref: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_PUBLIC_KEY_REF", description=""
    )
    otel_exporter_otlp_secret_key_ref: str | None = Field(
        default=None, alias="OTEL_EXPORTER_OTLP_SECRET_KEY_REF", description=""
    )
    otel_tls_profile: str | None = Field(
        default=None, alias="OTEL_TLS_PROFILE", description=""
    )
    otel_tls_profile_ref: str | None = Field(
        default=None, alias="OTEL_TLS_PROFILE_REF", description=""
    )
    permissions_signing_key_ref: str | None = Field(
        default=None,
        alias="PERMISSIONS_SIGNING_KEY_REF",
        description="Runtime secret reference for stable agent-identity HMAC authority (CONCEPT:AU-OS.state.cognitive-scheduler-preemption). Optional: when unset, governed execution self-provisions a durable, deployment- shared signing key into the engine's durable secret store (idempotent, atomic create-if-absent)....",
    )
    port: int = Field(default=9000, alias="PORT", description="")
    provider_configs: Any = Field(
        default_factory=lambda: {},
        alias="PROVIDER_CONFIGS",
        description="Deployment-owned, reference-only external provider profiles. Provider packages select a neutral profile and resolve it at their runtime boundary. The durable document never contains endpoint values, credentials, certificate material, personalized paths, or customized provider schemas.",
    )
    reactions: str = Field(default="1", alias="REACTIONS", description="")
    remote_oauth_providers: Any = Field(
        default=None,
        alias="REMOTE_OAUTH_PROVIDERS_JSON",
        description="Administrator-approved remote-MCP OAuth provider descriptors. The value is a JSON list in the process environment or XDG configuration; each entry is validated by ``ProviderDescriptor`` when the broker is built. No request or caller can add providers at runtime. Malformed JSON or non-object...",
    )
    remote_oauth_success_redirect_url: str | None = Field(
        default=None,
        alias="REMOTE_OAUTH_SUCCESS_REDIRECT_URL",
        description="Fixed administrator-configured URL used after a successful OAuth flow.",
    )
    role_routing: Any = Field(
        default_factory=lambda: {},
        alias="MODEL_ROLE_ROUTING",
        description="CONCEPT:AU-ORCH.routing.optional-role-override — optional role→{tier,tags} overrides for planner/generator/ learner/judge model selection. Empty roles fall back to the built-in default map in `models/model_registry.py`. Merged into the active registry when no registry-level override is present.",
    )
    sd35_url: str | None = Field(default=None, alias="SD35_URL", description="")
    secrets_backend: Literal["engine", "vault"] = Field(
        default="engine",
        alias="SECRETS_BACKEND",
        description="Secrets storage backend: encrypted engine storage (default) or Vault.",
    )
    seed: int | None = Field(default=None, alias="SEED", description="")
    sensitive_tool_patterns: list[str] = Field(
        default_factory=lambda: [
            ".*delete.*",
            ".*remove.*",
            ".*rm_.*",
            ".*rmdir.*",
            ".*drop.*",
            ".*truncate.*",
            ".*prune.*",
            ".*kill.*",
            ".*terminate.*",
            ".*reboot.*",
            ".*shutdown.*",
            ".*install.*",
            ".*uninstall.*",
            ".*redeploy.*",
            ".*bump.*",
            ".*create.*",
            ".*add.*",
            ".*post.*",
            ".*put.*",
            ".*insert.*",
            ".*upload.*",
            ".*ingest.*",
            ".*write.*",
            ".*update.*",
            ".*patch.*",
            ".*set.*",
            ".*reset.*",
            ".*clear.*",
            ".*revert.*",
            ".*replace.*",
            ".*rename.*",
            ".*move.*",
            ".*rotate.*",
            ".*start.*",
            ".*stop.*",
            ".*restart.*",
            ".*pause.*",
            ".*unpause.*",
            ".*execute.*",
            ".*shell.*",
            ".*run_shell.*",
            ".*run_command.*",
            ".*run_script.*",
            ".*run_code.*",
            ".*git_.*",
            ".*clone.*",
            ".*pull.*",
            ".*maintain.*",
            ".*setup.*",
            ".*build.*",
            ".*validate.*",
            ".*sync.*",
            ".*enable.*",
            ".*disable.*",
            ".*activate.*",
            ".*approve.*",
            ".*graphql.*",
            ".*mutation.*",
            ".*http.*",
            ".*eval.*",
            ".*exec.*",
            ".*compile.*",
            ".*socket.*",
            ".*connect.*",
            ".*os\\..*",
            ".*subprocess\\..*",
            ".*shutil\\..*",
        ],
        alias="SENSITIVE_TOOL_PATTERNS",
        description="",
    )
    skill_cert_graphos_command: list[str] = Field(
        default_factory=lambda: [], alias="SKILL_CERT_GRAPHOS_COMMAND", description=""
    )
    skill_cert_graphos_endpoint: str | None = Field(
        default=None, alias="SKILL_CERT_GRAPHOS_ENDPOINT", description=""
    )
    skill_cert_identity_authority_mode: Literal["ephemeral-https-loopback"] = Field(
        default="ephemeral-https-loopback",
        alias="SKILL_CERT_IDENTITY_AUTHORITY_MODE",
        description="Current exact certification owns one verified HTTPS loopback authority.",
    )
    skill_cert_promotion_evidence: str | None = Field(
        default=None, alias="SKILL_CERT_PROMOTION_EVIDENCE", description=""
    )
    skill_cert_release_spec: str | None = Field(
        default=None, alias="SKILL_CERT_RELEASE_SPEC", description=""
    )
    skill_cert_runtime_configuration: str | None = Field(
        default=None, alias="SKILL_CERT_RUNTIME_CONFIGURATION", description=""
    )
    skill_cert_runtime_profile: str | None = Field(
        default=None, alias="SKILL_CERT_RUNTIME_PROFILE", description=""
    )
    skill_validation_evidence_signer_command: list[str] = Field(
        default_factory=lambda: [],
        alias="SKILL_VALIDATION_EVIDENCE_SIGNER_COMMAND",
        description="",
    )
    skill_validation_evidence_verifier_command: list[str] = Field(
        default_factory=lambda: [],
        alias="SKILL_VALIDATION_EVIDENCE_VERIFIER_COMMAND",
        description="",
    )
    source_http_allow_browser_fetch: bool = Field(
        default=False, alias="SOURCE_HTTP_ALLOW_BROWSER_FETCH", description=""
    )
    source_http_allowed_private_hosts: list[str] = Field(
        default_factory=lambda: [],
        alias="SOURCE_HTTP_ALLOWED_PRIVATE_HOSTS",
        description="",
    )
    source_http_allowed_redirect_hosts: list[str] = Field(
        default_factory=lambda: [],
        alias="SOURCE_HTTP_ALLOWED_REDIRECT_HOSTS",
        description="",
    )
    source_http_max_redirects: int = Field(
        default=3, alias="SOURCE_HTTP_MAX_REDIRECTS", description=""
    )
    source_http_max_response_bytes: int = Field(
        default=10485760, alias="SOURCE_HTTP_MAX_RESPONSE_BYTES", description=""
    )
    spark_runner_endpoint: str = Field(
        default="",
        alias="SPARK_RUNNER_ENDPOINT",
        description="Spark driver pod's Spark UI endpoint (``services/spark``, Deployment name ``spark-runner``) -- an exec-driven job runner (``kubectl exec ... spark-sql.sh``), not a standalone Spark cluster with a submission API. The UI listener is disabled between jobs (``spark.ui.enabled=false``), so an...",
    )
    svd_url: str | None = Field(default=None, alias="SVD_URL", description="")
    timeout: float = Field(default=3600.0, alias="TIMEOUT", description="")
    tls_ca_bundle_ref: str | None = Field(
        default=None, alias="TLS_CA_BUNDLE_REF", description=""
    )
    tls_client_cert_ref: str | None = Field(
        default=None, alias="TLS_CLIENT_CERT_REF", description=""
    )
    tls_client_key_password_ref: str | None = Field(
        default=None, alias="TLS_CLIENT_KEY_PASSWORD_REF", description=""
    )
    tls_client_key_ref: str | None = Field(
        default=None, alias="TLS_CLIENT_KEY_REF", description=""
    )
    tls_profile: str | None = Field(default=None, alias="TLS_PROFILE", description="")
    tls_profile_ref: str | None = Field(
        default=None, alias="TLS_PROFILE_REF", description=""
    )
    tls_profiles_ref: str | None = Field(
        default=None, alias="TLS_PROFILES_REF", description=""
    )
    tls_proxy_url_ref: str | None = Field(
        default=None, alias="TLS_PROXY_URL_REF", description=""
    )
    tool_guard_mode: Literal["on", "strict"] = Field(
        default="strict", alias="TOOL_GUARD_MODE", description=""
    )
    trace_export_enabled: bool = Field(
        default=False,
        alias="TRACE_EXPORT_ENABLED",
        description="Enable trace export; the exporter endpoint/credentials remain runtime config.",
    )
    trino_endpoint: str = Field(
        default="",
        alias="TRINO_ENDPOINT",
        description="TLS Trino coordinator endpoint (``services/trino``) with the Iceberg REST connector pointed at Lakekeeper. Authenticated query composition rejects an explicit cleartext HTTP endpoint rather than downgrading transport security.",
    )
    whisper_url: str | None = Field(default=None, alias="WHISPER_URL", description="")
    xtts_url: str | None = Field(default=None, alias="XTTS_URL", description="")

    @field_validator("*", mode="before")
    @classmethod
    def _parse_structured(cls, value: Any, info: Any) -> Any:
        field = cls.model_fields.get(info.field_name)
        if field is None or not isinstance(value, str) or field.annotation is not Any:
            return value
        text = value.strip()
        if text[:1] in "[{":
            try:
                return json.loads(text)
            except ValueError:
                return value
        return value

    def __init__(self, **values: Any) -> None:
        if not values:
            values = _environment_values()
        super().__init__(**values)


def _environment_values() -> dict[str, Any]:
    out: dict[str, Any] = {}
    for info in GraphOSConfig.model_fields.values():
        alias = str(info.alias)
        raw = os.environ.get(alias)
        if raw is not None and raw != "":
            out[alias] = raw
    return out


@cache
def _adapter(name: str) -> TypeAdapter[Any]:
    return TypeAdapter(GraphOSConfig.model_fields[name].annotation or Any)


def read_field(name: str) -> Any:
    """Read one :class:`GraphOSConfig` field from the environment now."""
    info = GraphOSConfig.model_fields[name]
    raw = os.environ.get(str(info.alias))
    if raw is None or raw == "":
        return info.get_default(call_default_factory=True)
    value: Any = raw
    if info.annotation is Any and raw.strip()[:1] in "[{":
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw
    try:
        return _adapter(name).validate_python(value)
    except ValueError:
        return info.get_default(call_default_factory=True)


class _LiveConfig:
    """Attribute view over :class:`GraphOSConfig` that reads at call time.

    An attribute set on the view (a test override) wins until it is deleted.
    """

    def __init__(self) -> None:
        object.__setattr__(self, "_overrides", {})

    def __getattr__(self, name: str) -> Any:
        overrides = object.__getattribute__(self, "_overrides")
        if name in overrides:
            return overrides[name]
        if name in GraphOSConfig.model_fields:
            return read_field(name)
        raise AttributeError(name)

    def __setattr__(self, name: str, value: Any) -> None:
        self._overrides[name] = value

    def __delattr__(self, name: str) -> None:
        self._overrides.pop(name, None)


#: The live settings view every Graph OS module reads.
runtime_config: Any = _LiveConfig()


def _config_file() -> Path:
    return config_dir() / _CONFIG_FILE


def load_config(*, reload: bool = False) -> Path | None:
    """Project ``config.json`` into the process environment (set values win)."""
    from agent_connector_sdk.config import load_config as _sdk_load

    target = platform_config_file(_CONFIG_FILE)
    return _sdk_load(target, reload=reload) if target.is_file() else None


def _check_secret_policy(env_key: str, value: Any) -> None:
    if env_key.endswith(_CREDENTIAL_SUFFIXES) and not (
        isinstance(value, str) and value.startswith(_REFERENCE_SCHEMES)
    ):
        raise ValueError(
            f"configuration key {env_key} must hold a secret reference, not a value"
        )


def save_config_item(key: str, value: Any) -> str:
    """Persist one setting to ``config.json`` and the live environment.

    Returns the environment key. A credential-shaped key must hold a secret
    reference. The file is written atomically with mode ``0600``.
    """
    env_key = key.upper()
    _check_secret_policy(env_key, value)
    target = _config_file()
    with _write_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any] = {}
        if target.is_file():
            loaded = json.loads(target.read_text(encoding="utf-8") or "{}")
            if not isinstance(loaded, dict):
                raise ValueError(f"{target} must hold a JSON object")
            document = loaded
        if value is None:
            document.pop(env_key, None)
            os.environ.pop(env_key, None)
        else:
            document[env_key] = value
            os.environ[env_key] = value if isinstance(value, str) else json.dumps(value)
        write_private_mapping(target, document)
    return env_key


def write_private_mapping(path: Path, document: dict[str, Any]) -> None:
    """Write ``document`` as JSON to ``path`` atomically with mode ``0600``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2, sort_keys=True)
    os.replace(temp, path)


def plaintext_secret_keys(document: dict[str, Any]) -> list[str]:
    """Names of credential-shaped keys in ``document`` that hold a value, not a reference."""
    return sorted(
        str(key)
        for key, value in document.items()
        if str(key).upper().endswith(_CREDENTIAL_SUFFIXES)
        and value not in (None, "")
        and not (isinstance(value, str) and value.startswith(_REFERENCE_SCHEMES))
    )


_PRODUCTION_PROFILES = frozenset({"prod", "production"})


def is_production_profile(profile: str | None = None) -> bool:
    """True when ``profile`` (default ``APP_PROFILE``) names production."""
    value = setting("APP_PROFILE", "") if profile is None else profile
    return str(value or "").strip().lower() in _PRODUCTION_PROFILES
