"""Auto-extracted arbora MCP tools: analysis_tools (register_analysis_tools).

Split out of runtime._build_server to deepen the MCP surface into focused
modules without changing tool behavior or names.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from pydantic import Field

from arbora.api.errors import not_served
from arbora.api.evidence import EvidenceBundle
from arbora.core.environment import read_env, read_text
from arbora.mcp_server import runtime
from arbora.mcp_server.backing import engine_routing
from arbora.mcp_server.legacy_action_invoke import invoke_legacy_action
from arbora.security.config_sensitivity import (
    ENDPOINT_KEY_PARTS as _ENDPOINT_MCP_KEY_PARTS,
)
from arbora.security.config_sensitivity import (
    PATH_KEY_PARTS as _PATH_MCP_KEY_PARTS,
)
from arbora.security.config_sensitivity import (
    SENSITIVE_KEY_PARTS as _SENSITIVE_MCP_KEY_PARTS,
)
from arbora.security.config_sensitivity import (
    configuration_key_is_sensitive as _configuration_key_is_sensitive,
)
from arbora.security.config_sensitivity import (
    normalised_key_parts as _normalised_key_parts,
)
from arbora.security.config_sensitivity import (
    runtime_reference as _runtime_reference,
)
from arbora.security.error_surface import public_error_text

logger = logging.getLogger(__name__)


_MCP_REGISTRATION_MAX_BYTES = 128 * 1024
_MCP_CONFIG_MAX_BYTES = 1024 * 1024
_MCP_REGISTRATION_MAX_ITEMS = 2048
_MCP_REGISTRATION_MAX_DEPTH = 16
_MCP_SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_URI_LITERAL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
_INLINE_SECRET_RE = re.compile(
    r"(?i)^(?:bearer\s+\S+|basic\s+\S+|sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_-]{8,})$"
)
_EMAIL_LITERAL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_SAFE_INLINE_MCP_ENV_KEYS = frozenset(
    {
        "APP_PROFILE",
        "FASTMCP_LOG_LEVEL",
        "LOG_LEVEL",
        "MCP_CLIENT_AUTH",
        # Retired surface selector still present in older fleet configs: accepted
        # inline so those configs register, and never read.
        "MCP_TOOL_MODE",
        "NO_COLOR",
        "PYTHONUNBUFFERED",
        "UV_NATIVE_TLS",
    }
)
_SAFE_INLINE_MCP_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_analysis_core_handler: Any | None = None
_EXTERNAL_MAPPING_POLICY_FIELDS = frozenset(
    {
        "access",
        "edge_property_allowlist",
        "edge_type_overrides",
        "identity_property",
        "property_allowlist",
        "type_overrides",
    }
)
_EXTERNAL_MAPPING_POLICY_MAX_BYTES = 4 * 1024 * 1024


def _reject_nonfinite_json(_value: str) -> None:
    raise ValueError("non-finite JSON constants are not supported")


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON keys are not supported")
        value[key] = item
    return value


def _as_declaration_dict(configured: Any) -> dict[str, Any]:
    if hasattr(configured, "model_dump"):
        return configured.model_dump(exclude_defaults=True)
    if isinstance(configured, dict):
        return dict(configured)
    return {}


def _last_declaration_named(declarations: Any, name: str) -> dict[str, Any] | None:
    """The LAST entry in ``declarations`` whose ``name`` matches, or ``None``
    -- matches the original scan's "later entries win" behaviour exactly."""
    found: dict[str, Any] | None = None
    for configured in declarations:
        candidate = _as_declaration_dict(configured)
        if str(candidate.get("name") or "") == name:
            found = candidate
    return found


def _configured_external_graph_declaration(name: str) -> dict[str, Any]:
    """Resolve one reference-only declaration without returning it publicly."""

    from arbora.core.settings import runtime_config

    collections = (
        getattr(runtime_config, "external_graph_connectors", []) or [],
        getattr(runtime_config, "kg_connections", []) or [],
    )
    selected: dict[str, Any] = {}
    for declarations in collections:
        found = _last_declaration_named(declarations, name)
        if found is not None:
            selected = found
    return selected


def _load_external_mapping_policy_ref(store: Any, policy_ref: str) -> dict[str, Any]:
    """Resolve+parse the policy behind ``policy_ref``. An empty ref (no
    policy attached) resolves to an empty policy."""
    if not policy_ref:
        return {}
    raw = store.resolve_ref(policy_ref)
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw.encode("utf-8")) > _EXTERNAL_MAPPING_POLICY_MAX_BYTES
    ):
        raise ValueError("external mapping policy is missing or exceeds its bound")

    parsed = json.loads(
        raw,
        parse_constant=_reject_nonfinite_json,
        object_pairs_hook=_reject_duplicate_json_keys,
    )
    if not isinstance(parsed, dict):
        raise ValueError("external mapping policy must be an object")
    return parsed


def _validate_external_mapping_policy_fields(policy: dict[str, Any]) -> None:
    if set(policy).difference(_EXTERNAL_MAPPING_POLICY_FIELDS):
        raise ValueError("external mapping policy contains unsupported inline material")
    for field in ("access", "type_overrides", "edge_type_overrides"):
        if field in policy and not isinstance(policy[field], dict):
            raise ValueError("external mapping policy has an invalid mapping field")


def _validate_external_mapping_policy_access(policy: dict[str, Any]) -> None:
    access = policy.get("access")
    if not isinstance(access, dict):
        return
    if set(access).difference({"group_ids", "is_public", "markings"}):
        raise ValueError(
            "external mapping policy access cannot contain inline identities"
        )
    if "is_public" in access and not isinstance(access["is_public"], bool):
        raise ValueError("external mapping policy access is invalid")
    for field in ("group_ids", "markings"):
        value = access.get(field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError("external mapping policy access is invalid")


def _validate_external_mapping_policy_type_overrides(policy: dict[str, Any]) -> None:
    for field in ("type_overrides", "edge_type_overrides"):
        value = policy.get(field)
        if isinstance(value, dict) and any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in value.items()
        ):
            raise ValueError("external mapping policy has an invalid type mapping")


def _validate_external_mapping_policy_allowlists(policy: dict[str, Any]) -> None:
    for field in ("property_allowlist", "edge_property_allowlist"):
        value = policy.get(field)
        if value is not None and (
            not isinstance(value, list)
            or any(not isinstance(item, str) for item in value)
        ):
            raise ValueError("external mapping policy has an invalid allowlist")


def _validate_external_mapping_policy_identity(policy: dict[str, Any]) -> None:
    if "identity_property" in policy and not isinstance(
        policy["identity_property"], str
    ):
        raise ValueError("external mapping policy has an invalid identity property")


def _resolved_external_mapping_policy(
    store: Any, declaration: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Resolve, validate, and hash a property-graph policy behind its ref."""

    policy_ref = str(declaration.get("mapping_policy_ref") or "")
    policy = _load_external_mapping_policy_ref(store, policy_ref)

    _validate_external_mapping_policy_fields(policy)
    _validate_external_mapping_policy_access(policy)
    _validate_external_mapping_policy_type_overrides(policy)
    _validate_external_mapping_policy_allowlists(policy)
    _validate_external_mapping_policy_identity(policy)

    raise not_served(
        "knowledge_graph.ingestion.external_graph_schema", "MEDULLA-REPO-INGEST-R651"
    )


async def execute_focused_analysis(
    action: str,
    query: str = "",
    top_k: int = 10,
    node_id: str = "",
    depth: int = 2,
    target: str = "",
) -> EvidenceBundle:
    """Execute one canonical focused action through the shared analysis kernel."""

    if _analysis_core_handler is None:
        raise RuntimeError("analysis tools are not registered")
    raw = await _analysis_core_handler(
        action=action,
        query=query,
        top_k=top_k,
        node_id=node_id,
        depth=depth,
        target=target,
    )
    return EvidenceBundle.from_payload(raw, operation=action)


_SENSITIVE_MCP_OPTION_NAMES = frozenset(
    {
        "config",
        "connection-string",
        "dsn",
        "env",
        "h",
        "header",
        "proxy",
    }
)


def _mcp_option_is_sensitive(option_name: str) -> bool:
    return _configuration_key_is_sensitive(option_name) or (
        option_name.lower() in _SENSITIVE_MCP_OPTION_NAMES
    )


def _process_mcp_arg(argument: Any, expects_reference: bool) -> bool:
    """Validate one MCP command-line argument. Returns the new
    ``expects_reference`` state to carry into the NEXT argument."""
    if not isinstance(argument, str):
        raise ValueError("MCP server arguments must be strings")
    if expects_reference:
        if not _runtime_reference(argument):
            raise ValueError(
                "sensitive MCP command arguments must be runtime references"
            )
        return False
    if not argument.startswith("-"):
        return False
    option, separator, inline_value = argument.partition("=")
    option_name = option.lstrip("-")
    option_sensitive = _mcp_option_is_sensitive(option_name)
    if option_sensitive and separator:
        if not _runtime_reference(inline_value):
            raise ValueError(
                "sensitive MCP command arguments must be runtime references"
            )
        return False
    if option_sensitive:
        return True
    return False


def _validate_mcp_args(args: Any) -> None:
    if args is not None and not isinstance(args, list):
        raise ValueError("MCP server args must be a list")
    expects_reference = False
    for argument in args or []:
        expects_reference = _process_mcp_arg(argument, expects_reference)
    if expects_reference:
        raise ValueError("sensitive MCP command argument is missing its value")


def _mcp_key_requires_reference(key: str) -> bool:
    parts = _normalised_key_parts(key)
    if parts & (
        _SENSITIVE_MCP_KEY_PARTS | _ENDPOINT_MCP_KEY_PARTS | _PATH_MCP_KEY_PARTS
    ):
        return True
    if "id" in parts and bool(
        parts & {"actor", "agent", "client", "identity", "tenant", "user"}
    ):
        return True
    if "key" in parts and bool(
        parts
        & {
            "api",
            "auth",
            "client",
            "encryption",
            "hmac",
            "private",
            "signing",
            "tls",
        }
    ):
        return True
    return False


def _mcp_env_value_is_safe_inline(key: str, value: str) -> bool:
    return (
        key.upper() in _SAFE_INLINE_MCP_ENV_KEYS
        and _SAFE_INLINE_MCP_ENV_VALUE_RE.fullmatch(value) is not None
    )


def _looks_like_sensitive_literal(value: str) -> bool:
    return bool(
        _URI_LITERAL_RE.match(value)
        or value.startswith(("/", "~/", "~\\"))
        or _WINDOWS_ABSOLUTE_PATH_RE.match(value)
        or _INLINE_SECRET_RE.match(value)
        or _EMAIL_LITERAL_RE.match(value)
    )


def _validate_mcp_scalar_value(
    value: Any, key: str, in_env: bool, requires_reference: bool
) -> None:
    if not isinstance(value, str) or not value:
        if requires_reference and value not in (None, ""):
            raise ValueError("sensitive MCP values must be runtime references")
        return
    if _runtime_reference(value):
        return
    if in_env and not _mcp_env_value_is_safe_inline(key, value):
        raise ValueError(
            "MCP environment values must be runtime references unless the "
            "setting is a bounded non-sensitive mode"
        )
    if requires_reference:
        raise ValueError("sensitive MCP values must be runtime references")
    if _looks_like_sensitive_literal(value):
        raise ValueError(
            "endpoint, credential, identity, and path literals are not "
            "durable MCP configuration"
        )


def _validate_mcp_server_definition(definition: Any) -> dict[str, Any]:
    """Validate one durable MCP server declaration without retaining literals.

    Commands and neutral feature flags remain inline.  Endpoint, credential,
    identity, TLS/path material, and values which visibly look like credentials
    or PII must be runtime references.  The recursive bounds also prevent a
    small administrative request from causing excessive parsing/walk work.
    """

    if not isinstance(definition, dict):
        raise ValueError("MCP server definition must be an object")

    _validate_mcp_args(definition.get("args"))

    seen = 0

    def _walk(
        value: Any, *, key: str = "", depth: int = 0, in_env: bool = False
    ) -> None:
        nonlocal seen
        seen += 1
        if seen > _MCP_REGISTRATION_MAX_ITEMS:
            raise ValueError("MCP server definition is too large")
        if depth > _MCP_REGISTRATION_MAX_DEPTH:
            raise ValueError("MCP server definition is too deeply nested")

        requires_reference = _mcp_key_requires_reference(key)

        if isinstance(value, dict):
            for child_key, child_value in value.items():
                if not isinstance(child_key, str) or len(child_key) > 128:
                    raise ValueError("MCP server definition contains an invalid key")
                _walk(
                    child_value,
                    key=child_key,
                    depth=depth + 1,
                    in_env=in_env or key.lower() == "env",
                )
            return
        if isinstance(value, list):
            for child in value:
                _walk(child, key=key, depth=depth + 1, in_env=in_env)
            return
        _validate_mcp_scalar_value(value, key, in_env, requires_reference)

    _walk(definition)
    return definition


def _resolve_requested_mcp_config_path(root: Path, configured: str) -> Path:
    """Parse the ``MCP_CONFIG`` setting into a candidate path, absolute (and
    rooted under ``root`` when given relatively). Rejects null bytes,
    unexpanded env vars, Windows absolute paths, and ``..`` traversal."""
    if configured and ("\x00" in configured or "$" in configured):
        raise ValueError("MCP_CONFIG must resolve before registration")
    if _WINDOWS_ABSOLUTE_PATH_RE.match(configured):
        raise PermissionError("MCP config must use the active workspace namespace")
    requested = Path(configured).expanduser() if configured else Path("mcp_config.json")
    if ".." in requested.parts:
        raise PermissionError("MCP config traversal is not permitted")
    candidate = requested if requested.is_absolute() else root / requested
    return candidate.absolute()


def _assert_mcp_config_path_in_workspace(candidate: Path, root: Path) -> Path:
    try:
        return candidate.relative_to(root)
    except ValueError:
        raise PermissionError(
            "MCP config must be inside the active workspace"
        ) from None


def _assert_no_symlink_components(root: Path, relative: Path) -> None:
    current = root
    for component in relative.parts:
        current = current / component
        if current.is_symlink():
            raise PermissionError("symlinked MCP config paths are not writable")


def _assert_resolved_mcp_config_in_workspace(candidate: Path, root: Path) -> None:
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise PermissionError(
            "MCP config must resolve inside the active workspace"
        ) from None


def _workspace_mcp_config_path() -> Path:
    """Resolve the active MCP config strictly beneath the declared workspace.

    Registration is a workspace-scoped write operation. It uses only ``MCP_CONFIG``
    (when set) or ``<workspace>/mcp_config.json`` and rejects traversal and every
    existing symlink component.
    """

    workspace = str(read_env("WORKSPACE_PATH") or "").strip()
    root = (Path(workspace) if workspace else Path.cwd()).resolve(strict=True)
    configured = str(read_env("MCP_CONFIG", "") or "").strip()
    candidate = _resolve_requested_mcp_config_path(root, configured)
    relative = _assert_mcp_config_path_in_workspace(candidate, root)
    if candidate.suffix.lower() != ".json":
        raise ValueError("MCP config must be a JSON file")
    _assert_no_symlink_components(root, relative)
    _assert_resolved_mcp_config_in_workspace(candidate, root)
    return candidate


def _read_mcp_config_document(path: Path) -> dict[str, Any]:
    """Read a bounded regular JSON file without following the final symlink."""

    import stat

    if path.is_symlink():
        raise PermissionError("symlinked MCP config files are not readable")
    if not path.exists():
        return {}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("MCP config must be a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            raw = stream.read(_MCP_CONFIG_MAX_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw.encode("utf-8")) > _MCP_CONFIG_MAX_BYTES:
        raise ValueError("MCP config exceeds the size limit")
    document = json.loads(raw) if raw.strip() else {}
    if not isinstance(document, dict):
        raise ValueError("MCP config must be a JSON object")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("mcpServers must be a JSON object")
    return document


def _atomic_private_json_write(path: Path, document: dict[str, Any]) -> None:
    """Atomically replace ``path`` with a private regular JSON file."""

    import tempfile

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise PermissionError("symlinked MCP config paths are not writable")
    descriptor, raw_tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    tmp_path = Path(raw_tmp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if path.parent.is_symlink() or path.is_symlink():
            raise PermissionError("symlinked MCP config paths are not writable")
        os.replace(tmp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            # The atomic temp file is already created private on POSIX.  Some
            # Windows filesystems do not implement POSIX mode changes.
            pass
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _register_mcp_server(name: str, raw_definition: str) -> None:
    """Validate and atomically persist one named MCP server declaration."""

    if not _MCP_SERVER_NAME_RE.fullmatch((name or "").strip()):
        raise ValueError("invalid MCP server name")
    encoded = (raw_definition or "").encode("utf-8")
    if not encoded or len(encoded) > _MCP_REGISTRATION_MAX_BYTES:
        raise ValueError("MCP server definition has an invalid size")
    definition = _validate_mcp_server_definition(json.loads(raw_definition))
    path = _workspace_mcp_config_path()
    document = _read_mcp_config_document(path)
    servers = document.setdefault("mcpServers", {})
    for existing_name, existing_definition in servers.items():
        if not _MCP_SERVER_NAME_RE.fullmatch(str(existing_name)):
            raise ValueError("existing MCP configuration has an invalid server name")
        _validate_mcp_server_definition(existing_definition)
    servers[name.strip()] = definition
    _atomic_private_json_write(path, document)


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


def _configure_add_connection_default_spec(config_key):
    # extracted from 'add_connection' (CX-AU-03: split for CCN). AgentConfig
    # is the reference-only declarative plane; an operator may activate one
    # of those declarations by alias without copying any profile reference
    # into MCP arguments or traces. Returns (spec, error_json_or_None).
    try:
        candidate = _configured_external_graph_declaration(config_key)
    except Exception as exc:
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


def _configure_add_connection_spec(action, config_key, config_value, registry):
    # action(s): 'add_connection'
    try:
        spec = json.loads(config_value) if config_value else {}
    except Exception:
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
        spec, err = _configure_add_connection_default_spec(config_key)
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


def _configure_remove_connection(action, config_key, registry):
    # action(s): 'remove_connection'
    removed = registry.remove(config_key)
    if removed:
        from arbora.core.settings import save_config_item

        save_config_item("kg_connections", registry.export_specs())
    return json.dumps(
        {
            "status": "success" if removed else "not_found",
            "action": action,
            "connection": config_key,
            "persisted": bool(removed),
        }
    )


def _configure_action_add_connection(action, config_key, config_value):
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
            action, config_key, config_value, registry
        )
    if action == "remove_connection":
        return _configure_remove_connection(action, config_key, registry)
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


def _configure_mapping_ingest(action, config_key, config_value, registry):
    # action(s): 'ingest_connection'
    try:
        options = json.loads(config_value) if config_value else {}
    except Exception:
        return json.dumps({"error": "config_value must be a JSON object"})
    if not isinstance(options, dict):
        return json.dumps({"error": "config_value must be a JSON object"})
    try:
        _configured_external_graph_declaration(config_key)
    except Exception as exc:
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
    except Exception:
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


def _configure_action_approve_connection_mapping(action, config_key, config_value):
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
        return _configure_mapping_ingest(action, config_key, config_value, registry)
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
    except Exception:
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


def _configure_action_health(action, config_key, config_value):
    # action(s): 'health'
    from arbora.observability.runtime_health import collect_health

    return json.dumps(collect_health(), default=str)


def _configure_action_system_doctor(action, config_key, config_value):
    # action(s): 'system_doctor'
    from arbora.deployment import run_doctor

    opts, err = _configure_parse_json_opts(config_value)
    if err:
        return err
    return json.dumps(
        run_doctor(
            opts.get("only"),
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
        return json.dumps(
            {
                "error": "configuration doctor failed",
                "error_type": type(exc).__name__,
            }
        )


_CONFIGURE_ACTION_DISPATCH = {
    "set_secret": _configure_action_set_secret,
    "vault_sync": _configure_action_vault_sync,
    "register_mcp": _configure_action_register_mcp,
    "add_connection": _configure_action_add_connection,
    "remove_connection": _configure_action_add_connection,
    "list_connections": _configure_action_add_connection,
    "approve_connection_mapping": _configure_action_approve_connection_mapping,
    "connection_mapping_status": _configure_action_approve_connection_mapping,
    "discover_connection_schema": _configure_action_approve_connection_mapping,
    "external_graph_doctor": _configure_action_approve_connection_mapping,
    "profile_connection": _configure_action_approve_connection_mapping,
    "ingest_connection": _configure_action_approve_connection_mapping,
    "propose_connection_mapping": _configure_action_approve_connection_mapping,
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
    "health": _configure_action_health,
    "system_doctor": _configure_action_system_doctor,
    "preflight": _configure_action_preflight,
    "install_hooks": _configure_action_install_hooks,
    "uninstall_hooks": _configure_action_uninstall_hooks,
    "doctor": _configure_action_doctor,
}


def register_analysis_tools(mcp):
    """Register the analysis_tools group on the given FastMCP server."""

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
            return public_error_text(e)

    global _analysis_core_handler
    _analysis_core_handler = _run_analysis_action

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
            action=action,
            query=query,
            top_k=top_k,
            node_id=node_id,
            depth=depth,
            target=target,
        )

    runtime.REGISTERED_TOOLS["graph_analyze"] = graph_analyze

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
        handler = _CONFIGURE_ACTION_DISPATCH.get(action)
        if handler is None:
            return json.dumps({"error": "unknown configuration action"})
        try:
            return handler(action, config_key, config_value)
        except PermissionError:
            # Authorization and filesystem-boundary denials are policy results,
            # not successful MCP payloads.  Preserve fail-closed dispatch.
            raise PermissionError("configuration operation denied") from None
        except Exception as exc:
            logger.warning("graph_configure operation failed: %s", exc)
            return json.dumps(
                {
                    "error": "configuration operation failed",
                    "error_type": type(exc).__name__,
                }
            )

    runtime.REGISTERED_TOOLS["graph_configure"] = graph_configure
