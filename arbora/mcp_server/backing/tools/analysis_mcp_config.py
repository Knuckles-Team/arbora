"""The MCP server definitions an analysis may register: their validation, the workspace-confined config path and the private atomic write (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from arbora.core.environment import read_env
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
