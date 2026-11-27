"""Auto-extracted arbora MCP tools: analysis_tools (register_analysis_tools).

Split out of runtime._build_server to deepen the MCP surface into focused
modules without changing tool behavior or names.
"""

from __future__ import annotations

import json
import logging
from functools import partial
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.domain.mcp_server.backing import engine_routing
from arbora.api.errors import not_served
from arbora.api.evidence import EvidenceBundle
from arbora.core.environment import read_text
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.backing.tools.analysis_mapping_policy import (
    _configured_external_graph_declaration,
    _resolved_external_mapping_policy,
)
from arbora.mcp_server.backing.tools.analysis_mcp_config import (
    _register_mcp_server,
)
from arbora.mcp_server.host_caches import AnalysisCore, ToolTable
from arbora.mcp_server.legacy_action_invoke import invoke_legacy_action
from arbora.observability.state import HealthState
from arbora.security.config_sensitivity import (
    configuration_key_is_sensitive as _configuration_key_is_sensitive,
)
from arbora.security.error_surface import public_error_text

logger = logging.getLogger(__name__)


async def execute_focused_analysis(
    core: AnalysisCore,
    action: str,
    query: str = "",
    top_k: int = 10,
    node_id: str = "",
    depth: int = 2,
    target: str = "",
) -> EvidenceBundle:
    """Execute one canonical focused action through the shared analysis kernel."""

    raw = await core.run(
        action=action,
        query=query,
        top_k=top_k,
        node_id=node_id,
        depth=depth,
        target=target,
    )
    return EvidenceBundle.from_payload(raw, operation=action)


logger = logging.getLogger(__name__)


# CX-AU-03: _run_analysis_action's if/elif action-dispatch chain (CCN 289)
# extracted into one module-level async handler per action, called through
# a dict dispatch table. Mechanical extract-method: each handler's body is
# the original branch body, moved verbatim (reindented) with a uniform
# (engine, action, query, top_k, node_id, depth, target) signature so the
# dispatcher needs no per-branch call-site logic. Behaviour-preserving;
# see tests/characterization/test_run_analysis_action_characterization.py.


async def _analysis_action_readiness(
    engine, action, query, top_k, node_id, depth, target
):
    # action(s): 'readiness'
    raise not_served(
        "knowledge_graph.readiness",
        "arbora/observability/readiness.py",
        owner="arbora",
    )


# CX-AU-03: graph_configure's sequential if/elif action-dispatch chain
# (CCN 244) extracted into one module-level function per action, called
# through a dict dispatch table. Mechanical extract-method: each handler's
# body is the original branch body, moved verbatim (reindented). Some
# branches (e.g. approve_connection_mapping's internal sub-dispatch, the
# profile_flow shared tail) required a second decomposition pass; each
# split is noted inline. Behaviour-preserving; see
# tests/characterization/test_graph_configure_characterization.py.


def _configure_action_set_secret(action, config_key, config_value):
    # action(s): 'set_secret'
    from arbora.security.secrets import (
        create_secrets_client,
    )

    create_secrets_client().set(config_key, config_value)
    return json.dumps({"status": "success", "action": "set_secret", "stored": True})


def _configure_action_vault_sync(action, config_key, config_value):
    # action(s): 'vault_sync'
    from arbora.security.secrets import (
        create_secrets_client,
    )

    payload = json.loads(config_value) if config_value else {}
    env_keys = payload.get("env_keys", [])
    client = create_secrets_client()
    result = client.vault_sync(
        config_key,
        env_keys,
        values=payload.get("values"),
        overwrite=bool(payload.get("overwrite", False)),
    )
    result.update({"status": "success", "action": "vault_sync"})
    return json.dumps(result)


def _configure_action_register_mcp(action, config_key, config_value):
    # action(s): 'register_mcp'
    try:
        _register_mcp_server(config_key, config_value)
    except PermissionError:
        raise
    except Exception as exc:
        logger.warning("configure action failed", exc_info=True)
        return json.dumps(
            {
                "error": "MCP registration rejected",
                "error_type": type(exc).__name__,
            }
        )
    return json.dumps(
        {
            "status": "success",
            "action": "register_mcp",
            "server": config_key,
        }
    )


def _configure_add_connection_default_spec(config_key, config):
    # extracted from 'add_connection' (CX-AU-03: split for CCN). AgentConfig
    # is the reference-only declarative plane; an operator may activate one
    # of those declarations by alias without copying any profile reference
    # into MCP arguments or traces. Returns (spec, error_json_or_None).
    try:
        candidate = _configured_external_graph_declaration(config_key, config)
    except Exception as exc:
        logger.warning("configure action failed", exc_info=True)
        return {}, json.dumps(
            {
                "error": "configured connection lookup failed",
                "error_type": type(exc).__name__,
            }
        )
    if candidate:
        candidate.pop("name", None)
        candidate["role"] = str(candidate.get("role") or "read")
        return candidate, None
    return {}, None


def _configure_add_connection_spec(action, config_key, config_value, registry, config):
    # action(s): 'add_connection'
    try:
        spec = json.loads(config_value) if config_value else {}
    except (ValueError, TypeError):
        return json.dumps({"error": "config_value must contain valid JSON"})
    if not isinstance(spec, dict):
        return json.dumps(
            {"error": "config_value must be a JSON object (backend spec)"}
        )
    declared_name = spec.pop("name", None)
    if declared_name not in (None, config_key):
        return json.dumps(
            {
                "error": (
                    "connection name is authoritative in config_key; "
                    "a payload name cannot select another alias"
                )
            }
        )
    if not spec:
        spec, err = _configure_add_connection_default_spec(config_key, config)
        if err:
            return err
    if not spec:
        return json.dumps(
            {
                "error": (
                    "add_connection requires a reference-only JSON "
                    "declaration or a matching AgentConfig alias"
                )
            }
        )
    exc = not_served(
        "knowledge_graph.core.connection_registry", "MEDULLA-UNIFIED-DATA-PLANE-R612"
    )
    return json.dumps(
        {
            "error": "connection registration is not persistence-safe",
            "error_type": type(exc).__name__,
        }
    )


def _configure_remove_connection(action, config_key, registry, config):
    # action(s): 'remove_connection'
    removed = registry.remove(config_key)
    if removed:
        config.save_item("kg_connections", registry.export_specs())
    return json.dumps(
        {
            "status": "success" if removed else "not_found",
            "action": action,
            "connection": config_key,
            "persisted": bool(removed),
        }
    )


def _configure_action_add_connection(config, action, config_key, config_value):
    # action(s): 'add_connection', 'remove_connection', 'list_connections'
    registry = engine_routing.get_connection_registry()
    if action == "list_connections":
        return json.dumps(registry.status(), default=str)
    if not config_key:
        return json.dumps(
            {"error": f"config_key (connection name) required for {action}"}
        )
    if action == "add_connection":
        return _configure_add_connection_spec(
            action, config_key, config_value, registry, config
        )
    if action == "remove_connection":
        return _configure_remove_connection(action, config_key, registry, config)
    return None


def _configure_mapping_status_lookup(config_key, config_value, registry, store):
    # action(s): 'connection_mapping_status'
    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )


def _configure_mapping_approve(config_key, config_value, store):
    # action(s): 'approve_connection_mapping'
    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )


def _configure_mapping_approve_status(action, config_key, config_value, registry):
    # action(s): 'approve_connection_mapping', 'connection_mapping_status'
    from arbora.security.secrets import (
        create_secrets_client,
    )

    store = create_secrets_client()
    if action == "connection_mapping_status":
        return _configure_mapping_status_lookup(
            config_key, config_value, registry, store
        )
    return _configure_mapping_approve(config_key, config_value, store)


def _configure_mapping_ingest(action, config_key, config_value, registry, config):
    # action(s): 'ingest_connection'
    try:
        options = json.loads(config_value) if config_value else {}
    except (ValueError, TypeError):
        return json.dumps({"error": "config_value must be a JSON object"})
    if not isinstance(options, dict):
        return json.dumps({"error": "config_value must be a JSON object"})
    try:
        _configured_external_graph_declaration(config_key, config)
    except Exception as exc:
        logger.warning("configure action failed", exc_info=True)
        return json.dumps(
            {
                "error": "configured source lookup failed",
                "error_type": type(exc).__name__,
            }
        )
    refusal = not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )
    return json.dumps(
        {
            "error": "external source declaration is invalid",
            "error_type": type(refusal).__name__,
        }
    )


def _configure_profile_setup(config_key, config_value, registry):
    # extracted from the profile_flow (CX-AU-03: split for CCN) -- prologue
    # shared by discover/doctor/profile/propose. Returns
    # (ext_engine, options, backend, backend_kind, connector_config,
    #  max_types, max_depth, error_json_or_None).
    try:
        registry.get_engine(config_key)
    except Exception as e:
        logger.warning("configure action failed", exc_info=True)
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            json.dumps(
                {
                    "error": "external graph connection unavailable",
                    "error_type": type(e).__name__,
                }
            ),
        )
    try:
        options = json.loads(config_value) if config_value else {}
    except (ValueError, TypeError):
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            json.dumps({"error": "config_value must be a JSON object"}),
        )
    if not isinstance(options, dict):
        return (
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            json.dumps({"error": "config_value must be a JSON object"}),
        )
    registry.backend_kind(config_key)
    exc = not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )
    return (
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        json.dumps(
            {
                "error": "external source declaration is invalid",
                "error_type": type(exc).__name__,
            }
        ),
    )


def _configure_profile_validate_options(action, backend_kind, options):
    # extracted from the profile_flow (CX-AU-03: split for CCN).
    if (
        backend_kind == "graphql"
        and action
        in {
            "discover_connection_schema",
            "external_graph_doctor",
            "profile_connection",
        }
        and set(options).difference({"max_depth", "max_types"})
    ):
        return json.dumps(
            {
                "error": (
                    "GraphQL discovery actions accept bounded discovery "
                    "limits only; source material comes from runtime refs"
                )
            }
        )
    if (
        backend_kind != "graphql"
        and action
        in {
            "discover_connection_schema",
            "external_graph_doctor",
            "profile_connection",
        }
        and set(options).difference({"max_types"})
    ):
        return json.dumps(
            {
                "error": (
                    "external graph discovery actions accept a bounded "
                    "max_types value only; source material stays behind "
                    "configured runtime refs"
                )
            }
        )
    return None


def _configure_profile_discover(
    action, backend_kind, ext_engine, backend, config_key, max_types, max_depth
):
    # action(s): 'discover_connection_schema', 'profile_connection'
    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )


def _configure_profile_policy(backend_kind, store, connector_config):
    # extracted from the profile_flow (CX-AU-03: split for CCN). Returns
    # (runtime_policy, runtime_policy_digest, error_json_or_None).
    runtime_policy: dict[str, Any] = {}
    runtime_policy_digest = ""
    if backend_kind != "graphql":
        try:
            (
                runtime_policy,
                runtime_policy_digest,
            ) = _resolved_external_mapping_policy(store, connector_config)
        except Exception as exc:
            logger.warning("configure action failed", exc_info=True)
            return (
                {},
                "",
                json.dumps(
                    {
                        "error": "secret-backed mapping policy resolution failed",
                        "error_type": type(exc).__name__,
                    }
                ),
            )
    return runtime_policy, runtime_policy_digest, None


def _configure_profile_doctor(
    backend_kind,
    ext_engine,
    config_key,
    store,
    max_types,
    max_depth,
    backend,
    runtime_policy_digest,
):
    # action(s): 'external_graph_doctor'
    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )


def _configure_profile_propose_graphql(
    ext_engine, config_key, store, max_types, max_depth, options
):
    # action(s): 'propose_connection_mapping' (GraphQL backend)
    if set(options).difference({"max_depth", "max_types"}):
        return json.dumps(
            {
                "error": (
                    "GraphQL mapping proposals resolve query, mapping, "
                    "governance, auth, and TLS policy from runtime refs"
                )
            }
        )
    exc = not_served(
        "knowledge_graph.ingestion.graphql_connection", "MEDULLA-REPO-INGEST-R651"
    )
    return json.dumps(
        {
            "error": "GraphQL mapping proposal failed",
            "error_type": type(exc).__name__,
        }
    )


def _configure_profile_propose_default(
    options,
    connector_config,
    registry,
    ext_engine,
    backend,
    config_key,
    store,
    runtime_policy,
    runtime_policy_digest,
    max_types,
):
    # action(s): 'propose_connection_mapping' (non-GraphQL backend)
    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )


def _configure_mapping_profile_flow(action, config_key, config_value, registry):
    # action(s): 'discover_connection_schema', 'external_graph_doctor', 'profile_connection', 'propose_connection_mapping'
    from arbora.security.secrets import (
        create_secrets_client,
    )

    (
        ext_engine,
        options,
        backend,
        backend_kind,
        connector_config,
        max_types,
        max_depth,
        err,
    ) = _configure_profile_setup(config_key, config_value, registry)
    if err:
        return err
    err = _configure_profile_validate_options(action, backend_kind, options)
    if err:
        return err
    if action in {"discover_connection_schema", "profile_connection"}:
        return _configure_profile_discover(
            action, backend_kind, ext_engine, backend, config_key, max_types, max_depth
        )
    store = create_secrets_client()
    runtime_policy, runtime_policy_digest, err = _configure_profile_policy(
        backend_kind, store, connector_config
    )
    if err:
        return err
    if action == "external_graph_doctor":
        return _configure_profile_doctor(
            backend_kind,
            ext_engine,
            config_key,
            store,
            max_types,
            max_depth,
            backend,
            runtime_policy_digest,
        )
    if backend_kind == "graphql":
        return _configure_profile_propose_graphql(
            ext_engine, config_key, store, max_types, max_depth, options
        )
    return _configure_profile_propose_default(
        options,
        connector_config,
        registry,
        ext_engine,
        backend,
        config_key,
        store,
        runtime_policy,
        runtime_policy_digest,
        max_types,
    )


def _configure_action_approve_connection_mapping(
    config, action, config_key, config_value
):
    # action(s): 'approve_connection_mapping', 'connection_mapping_status', 'discover_connection_schema', 'external_graph_doctor', 'profile_connection', 'ingest_connection', 'propose_connection_mapping'
    if not config_key:
        return json.dumps(
            {"error": f"config_key (connection name) required for {action}"}
        )
    registry = engine_routing.get_connection_registry()
    if action in {
        "approve_connection_mapping",
        "connection_mapping_status",
    }:
        return _configure_mapping_approve_status(
            action, config_key, config_value, registry
        )
    if action == "ingest_connection":
        return _configure_mapping_ingest(
            action, config_key, config_value, registry, config
        )
    return _configure_mapping_profile_flow(action, config_key, config_value, registry)


def _configure_action_mirror_status(action, config_key, config_value):
    # action(s): 'mirror_status', 'reconcile'
    raise not_served("knowledge_graph.backends", "MEDULLA-PLACEMENT-R611")


def _configure_action_legacy_backend_removed(action, config_key, config_value):
    # action(s): 'push_to_stardog', 'pull_from_stardog', 'stardog_sparql',
    # 'stardog_export_graph', 'stardog_import_graph', 'setup_databases',
    # 'verify_databases'. These all wired the legacy external SPARQL store
    # (Stardog/Jena Fuseki) and its database-setup CLI, both retired outright.
    # The action names stay reachable/discoverable on this surface and answer
    # with a typed, unambiguous refusal rather than an import error or a
    # silently vanished action.
    raise not_served("knowledge_graph.backends", "MEDULLA-PLACEMENT-R611")


def _configure_parse_json_opts(config_value):
    # extracted from 'generate_config' et al (CX-AU-03: split for CCN).
    # Returns (opts, error_json_or_None).
    try:
        opts = json.loads(config_value) if config_value else {}
    except (ValueError, TypeError):
        return None, json.dumps({"error": "config_value must contain valid JSON"})
    if not isinstance(opts, dict):
        return None, json.dumps({"error": "config_value must be a JSON object"})
    return opts, None


def _configure_action_generate_config(action, config_key, config_value):
    # action(s): 'generate_config', 'config_doctor', 'config_reference'
    from arbora.deployment import (
        config_doctor,
        config_reference,
        write_config,
    )

    opts, err = _configure_parse_json_opts(config_value)
    if err:
        return err
    if action == "config_reference":
        return json.dumps(config_reference(), default=str)
    # profile shortcut via config_key ('tiny'/'single-node-prod'/'enterprise')
    profile = opts.get("profile") or config_key or None
    if action == "generate_config":
        if opts.get("out"):
            return json.dumps({"error": "remote_path_not_allowed"})
        return json.dumps(
            write_config(
                profile or "tiny",
            ),
            default=str,
        )
    # config_doctor
    if opts.get("config"):
        return json.dumps({"error": "remote_path_not_allowed"})
    return json.dumps(config_doctor(profile), default=str)


def _configure_known_env_fields():
    # extracted from 'get_config'/'list_config' (CX-AU-03: split for CCN)
    from arbora.deployment import config_reference

    known: dict[str, dict] = {}
    for section in config_reference():
        for f in section.get("fields", []):
            known[str(f.get("env") or "").upper()] = f
    return known


def _configure_list_config(known):
    # action(s): 'list_config'
    out = {}
    for env_key, meta in known.items():
        val = read_text(env_key)
        out[env_key] = (
            "***" if (_configuration_key_is_sensitive(env_key, meta) and val) else val
        )
    return json.dumps({"config": out, "count": len(out)}, default=str)


def _configure_get_config_value(env_key, meta):
    # action(s): 'get_config'
    from arbora.deployment import is_restart_required

    val = read_text(env_key)
    if _configuration_key_is_sensitive(env_key, meta) and val:
        val = "***"
    return json.dumps(
        {
            "key": env_key,
            "value": val,
            "restart_required": is_restart_required(env_key),
        },
        default=str,
    )


def _configure_action_get_config(action, config_key, config_value):
    # action(s): 'get_config', 'list_config'
    known = _configure_known_env_fields()
    if action == "list_config":
        return _configure_list_config(known)
    if not config_key:
        return json.dumps({"error": f"config_key (env name) required for {action}"})
    env_key = config_key.upper()
    if env_key not in known:
        return json.dumps({"error": "Unknown config key (see config_reference)"})
    return _configure_get_config_value(env_key, known[env_key])


def _configure_action_health(health, action, config_key, config_value):
    # action(s): 'health'
    from arbora.observability.runtime_health import collect_health

    return json.dumps(collect_health(health), default=str)


def _configure_action_system_doctor(config, action, config_key, config_value):
    # action(s): 'system_doctor'
    from arbora.deployment import run_doctor

    opts, err = _configure_parse_json_opts(config_value)
    if err:
        return err
    return json.dumps(
        run_doctor(
            opts.get("only"),
            config=config,
            fix=opts.get("fix", False),
            live=opts.get("live", False),
        ),
        default=str,
    )


def _configure_action_preflight(action, config_key, config_value):
    # action(s): 'preflight'
    from arbora.deployment.preflight import run_preflight

    profile = config_key or "tiny"
    opts, err = _configure_parse_json_opts(config_value)
    if err:
        return err
    return json.dumps(
        run_preflight(profile, opts.get("components")),
        default=str,
    )


def _configure_action_install_hooks(action, config_key, config_value):
    # action(s): 'install_hooks'
    try:
        from arbora.ecosystem.hook_installer import HookInstaller

        installer = HookInstaller()
        agents = config_value.split(",") if config_value else None
        results = installer.install(agents)
        return json.dumps(
            {
                "status": "success",
                "results": results,
                "installed": installer.installed,
                "errors": installer.errors,
            }
        )
    except PermissionError:
        raise
    except Exception as exc:
        logger.warning("configure action failed", exc_info=True)
        return json.dumps(
            {
                "error": "hook installation failed",
                "error_type": type(exc).__name__,
            }
        )


def _configure_action_uninstall_hooks(action, config_key, config_value):
    # action(s): 'uninstall_hooks'
    try:
        from arbora.ecosystem.hook_installer import HookInstaller

        agents = config_value.split(",") if config_value else None
        results = HookInstaller().uninstall(agents)
        return json.dumps({"status": "success", "results": results})
    except PermissionError:
        raise
    except Exception as exc:
        logger.warning("configure action failed", exc_info=True)
        return json.dumps(
            {
                "error": "hook removal failed",
                "error_type": type(exc).__name__,
            }
        )


def _configure_action_doctor(action, config_key, config_value):
    # action(s): 'doctor'
    try:
        from arbora.ecosystem.hook_installer import HookInstaller

        return json.dumps(HookInstaller().doctor(), default=str)
    except PermissionError:
        raise
    except Exception as exc:
        logger.warning("configure action failed", exc_info=True)
        return json.dumps(
            {
                "error": "configuration doctor failed",
                "error_type": type(exc).__name__,
            }
        )


_CONFIGURE_ACTION_DISPATCH = MappingProxyType(
    {
        "set_secret": _configure_action_set_secret,
        "vault_sync": _configure_action_vault_sync,
        "register_mcp": _configure_action_register_mcp,
        "mirror_status": _configure_action_mirror_status,
        "reconcile": _configure_action_mirror_status,
        "push_to_stardog": _configure_action_legacy_backend_removed,
        "pull_from_stardog": _configure_action_legacy_backend_removed,
        "stardog_sparql": _configure_action_legacy_backend_removed,
        "stardog_export_graph": _configure_action_legacy_backend_removed,
        "stardog_import_graph": _configure_action_legacy_backend_removed,
        "setup_databases": _configure_action_legacy_backend_removed,
        "verify_databases": _configure_action_legacy_backend_removed,
        "generate_config": _configure_action_generate_config,
        "config_doctor": _configure_action_generate_config,
        "config_reference": _configure_action_generate_config,
        "get_config": _configure_action_get_config,
        "list_config": _configure_action_get_config,
        "preflight": _configure_action_preflight,
        "install_hooks": _configure_action_install_hooks,
        "uninstall_hooks": _configure_action_uninstall_hooks,
        "doctor": _configure_action_doctor,
    }
)


def register_analysis_tools(
    mcp,
    tools: ToolTable,
    core: AnalysisCore,
    health: HealthState,
    config: RuntimeConfig,
):
    """Register the analysis_tools group on the given FastMCP server."""

    configure_dispatch = {
        **_CONFIGURE_ACTION_DISPATCH,
        "health": partial(_configure_action_health, health),
        "add_connection": partial(_configure_action_add_connection, config),
        "remove_connection": partial(_configure_action_add_connection, config),
        "list_connections": partial(_configure_action_add_connection, config),
        "approve_connection_mapping": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "connection_mapping_status": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "discover_connection_schema": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "external_graph_doctor": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "profile_connection": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "ingest_connection": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "propose_connection_mapping": partial(
            _configure_action_approve_connection_mapping, config
        ),
        "system_doctor": partial(_configure_action_system_doctor, config),
    }

    async def _run_analysis_action(
        action: str = Field(
            default="inspect",
            description="Ops/structural action: inspect | enrichment_coverage | process_writeback | placement_plan | infra_sweep | security_scan. (Codebase→graph_code, research→graph_research, eval→graph_evaluate, Q&A→graph_explain, traces→graph_observe.)",
        ),
        query: str = Field(default="", description="Query or path for the analysis."),
        top_k: int = Field(
            default=10, description="Number of results or complexity budget."
        ),
        node_id: str = Field(
            default="",
            description="Specific node ID to analyze (e.g., for blast_radius).",
        ),
        depth: int = Field(
            default=2, description="Depth of traversal (e.g., for blast_radius)."
        ),
        target: str = Field(
            default="", description="Target for the analysis or inspection."
        ),
    ) -> str:
        """Execute complex analysis across the Knowledge Graph. Enables advanced semantic synthesis, causal dependency mapping, and structural inspection."""
        try:
            return await invoke_legacy_action(
                action,
                {
                    "query": query,
                    "top_k": top_k,
                    "node_id": node_id,
                    "depth": depth,
                    "target": target,
                },
            )
        except Exception as e:
            logger.warning("operation failed", exc_info=True)
            return public_error_text(e)

    core.bind(_run_analysis_action)

    @mcp.tool(
        name="graph_analyze",
        description=(
            "Structural and operational KG analysis. Actions: inspect | "
            "enrichment_coverage | process_writeback | placement_plan | "
            "infra_sweep | security_scan | distill_memory (KG-2.316/2.318 — "
            "export consolidated/procedural memory to a SFT/DPO/GRPO corpus and "
            "optionally submit=true to dispatch a live data-science-mcp train; "
            "poll_job_id=<id> reads a submitted job's status back) | readiness "
            "(GOC-02 — the arbora.readiness.v1 truthful engine/identity/catalog/"
            "source-sync/index/synthetic-query snapshot; readiness is never "
            "reported from liveness alone). Use graph_code, "
            "graph_research, graph_evaluate, graph_explain, or graph_observe for "
            "their focused domains. Returns the sole typed EvidenceBundle response."
        ),
        tags=["arbora", "analyze"],
    )
    async def graph_analyze(
        action: str = Field(
            default="inspect",
            description="inspect | enrichment_coverage | process_writeback | placement_plan | infra_sweep | security_scan | distill_memory | readiness",
        ),
        query: str = Field(default="", description="Query or path for the analysis."),
        top_k: int = Field(default=10, description="Result or complexity bound."),
        node_id: str = Field(default="", description="Optional anchor node id."),
        depth: int = Field(default=2, description="Traversal depth."),
        target: str = Field(default="", description="Analysis target."),
    ) -> EvidenceBundle:
        # CONCEPT:MERISTEM-KG.memory.memory-weights-distillation-export — "distill_memory" is handled
        # by the shared action core (_run_analysis_action) below but was left out
        # of this tool's own allowlist when graph_analyze's other ~24 actions were
        # split into the focused graph_code/graph_research/graph_evaluate/
        # graph_explain/graph_observe suite (analyze_suite.py); none of those
        # picked it up either, so it was unreachable from any live MCP/REST
        # surface. It doesn't fit those five focused domains, so it stays here
        # on graph_analyze's structural/operational surface.
        allowed = {
            "inspect",
            "enrichment_coverage",
            "process_writeback",
            "placement_plan",
            "infra_sweep",
            "security_scan",
            "distill_memory",
            # GOC-02: the arbora.readiness.v1 snapshot (engine, identity/policy,
            # catalog, source_sync, dense/sparse index, synthetic query canary —
            # see readiness.py's module docstring). Structural/operational, so it
            # lives on this surface rather than one of the five focused domains.
            "readiness",
        }
        if action not in allowed:
            return EvidenceBundle.from_payload(
                {
                    "error": "action belongs to a focused graph tool",
                    "action": action,
                },
                operation="graph_analyze",
            )
        return await execute_focused_analysis(
            core,
            action=action,
            query=query,
            top_k=top_k,
            node_id=node_id,
            depth=depth,
            target=target,
        )

    tools["graph_analyze"] = graph_analyze

    @mcp.tool(
        name="graph_configure",
        description="Manage backend configurations, system credentials, and tool registration within the unified agent ecosystem.",
        tags=["arbora", "configure"],
    )
    def graph_configure(
        action: str = Field(
            default="register_mcp",
            description=(
                "Configuration operation. Core actions: set_secret, vault_sync, "
                "frontend_contributions, "
                "register_mcp, install_hooks, uninstall_hooks, harness_fence, "
                "schema_pack, schema_candidates, add_connection, remove_connection, "
                "list_connections, mirror_status, reconcile, "
                "generate_config, config_doctor, config_reference, get_config, "
                "list_config, system_doctor, health, preflight, and "
                "doctor (hook-installer self-check; distinct from config_doctor/"
                "system_doctor). set_role_routing persists a role's model "
                "registry routing entry (config_value is the JSON RoleSpec "
                "payload). Universal "
                "external-source lifecycle actions are discover_connection_schema, "
                "propose_connection_mapping, approve_connection_mapping, "
                "connection_mapping_status, external_graph_doctor, "
                "profile_connection, and ingest_connection. Durable connection "
                "declarations accept neutral "
                "aliases and runtime secret references only; endpoint, credential, "
                "identity, query, TLS material, and local-path literals are rejected. "
                "GraphQL sources use a read-only runtime adapter; "
                "their connection, mapping, auth, TLS, and variables documents remain "
                "separate refs, and every ingest rechecks the approved schema and "
                "mapping-policy digests. push_to_stardog, pull_from_stardog, "
                "stardog_sparql, stardog_export_graph, stardog_import_graph, "
                "setup_databases, and verify_databases were retired with the "
                "legacy SPARQL backend and database-setup path; each name "
                "stays reachable and answers with a typed unavailable error."
            ),
        ),
        config_key: str = Field(
            default="",
            description="The key or ID of the configuration/secret (for 'schema_pack', the pack name e.g. 'research-state'; for connection actions, the connection name).",
        ),
        config_value: str = Field(
            default="",
            description="JSON string containing the payload or secret value.",
        ),
    ) -> str:
        """Manage backend configurations and abstract credentials. Allows dynamic registry updates and credential injection during agent provisioning."""
        handler = configure_dispatch.get(action)
        if handler is None:
            return json.dumps({"error": "unknown configuration action"})
        try:
            return handler(action, config_key, config_value)
        except PermissionError:
            # Authorization and filesystem-boundary denials are policy results,
            # not successful MCP payloads.  Preserve fail-closed dispatch.
            raise PermissionError("configuration operation denied") from None
        except Exception as exc:
            logger.warning("graph_configure operation failed: %s", exc, exc_info=True)
            return json.dumps(
                {
                    "error": "configuration operation failed",
                    "error_type": type(exc).__name__,
                }
            )

    tools["graph_configure"] = graph_configure
