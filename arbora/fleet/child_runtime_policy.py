"""The runtime child policy: how a child declaration is attested, materialized and prepared before it spawns (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import hashlib
import hmac
import importlib.metadata
import json
import logging
import re
import secrets
import typing as _typing

from arbora.core.settings import RuntimeConfig
from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    pass
from arbora.fleet.child_payload_bounds import (
    _ABSENT,
)

logger = logging.getLogger("mcp_multiplexer")
# The SOLE handle on the MCP protocol types in this module, for two reasons that
# both bite here. (1) MCP SDK v2 re-homed the whole `mcp.types` namespace into the
# standalone `mcp_types` distribution, so `import mcp.types` raises ImportError at
# module scope on an SDK v2 image — and this module is the fleet loader, so that
# takes every child server down with it. `mcp_types_module()` binds whichever the
# installed SDK ships. (2) `mcp` is also a common PARAMETER name in this file (see
# `_register_forwarder`), where it shadows the package outright; a `mcp_types.X`
# attribute chain there resolves against the parameter, not the SDK.
mcp_types = mcp_types_module()


_SESSION_KEY = secrets.token_bytes(32)


_RUNTIME_CHILD_POLICY_GROUP = "arbora.mcp_child_policies"


_RUNTIME_CHILD_POLICY_RE = re.compile(r"^[a-z][a-z0-9-]{1,62}$")


_RUNTIME_CHILD_POLICY_TRANSPORT_KEYS = frozenset(
    {
        "args",
        "command",
        "env",
        "headers",
        "provider_profile",
        "tls_profile",
        "tls_profile_ref",
        "transport",
        "url",
    }
)


_RUNTIME_CHILD_POLICY_INTERNAL_KEY = "_runtime_child_policy"


_RUNTIME_REF_RE = re.compile(
    r"^(?:env://[A-Z][A-Z0-9_]{0,127}|"
    r"(?:vault|secret)://[A-Za-z0-9][A-Za-z0-9_./#-]{0,511})$"
)


_ENV_TEMPLATE_RE = re.compile(r"\$\{(?:env:)?([A-Za-z_][A-Za-z0-9_]*)\}")


_SENSITIVE_CONFIG_KEY_RE = re.compile(
    r"(?:^|_)(?:AUTHORIZATION|COOKIE|CREDENTIAL|PASSWORD|SECRET|TOKEN|API_KEY|HMAC_KEY)(?:_|$)",
    re.IGNORECASE,
)


_PROVIDER_CHILD_ENV_KEYS = frozenset({"AGENT_PROVIDER_PROFILE", "PROVIDER_CONFIGS"})


def _sensitive_config_key(name: str) -> bool:
    normalized = str(name).replace("-", "_")
    return bool(_SENSITIVE_CONFIG_KEY_RE.search(normalized))


def _externalized_value(value: _typing.Any) -> bool:
    rendered = str(value or "")
    return bool(
        _RUNTIME_REF_RE.fullmatch(rendered) or _ENV_TEMPLATE_RE.search(rendered)
    )


def _validate_externalized_child_secrets(cfg: dict[str, _typing.Any]) -> None:
    """Reject credentials committed inline in a persistent child catalog."""
    for container_name in ("env", "headers"):
        values = cfg.get(container_name) or {}
        if not isinstance(values, dict):
            raise RuntimeError("MCP child configuration is invalid")
        for key, value in values.items():
            if container_name == "env" and str(key).upper() in _PROVIDER_CHILD_ENV_KEYS:
                raise RuntimeError(
                    "MCP child provider selection must use provider_profile"
                )
            if _sensitive_config_key(str(key)) and not _externalized_value(value):
                raise RuntimeError("MCP child credentials must use runtime references")


def _selected_child_provider_profile(
    cfg: dict[str, _typing.Any], *, is_remote: bool
) -> str | None:
    """Validate one stdio child's deployment-owned provider profile."""

    selected = cfg.get("provider_profile")
    if selected in (None, ""):
        return None
    profile_name = str(selected).strip()
    if (
        profile_name != selected
        or re.fullmatch(r"[a-z][a-z0-9-]{1,62}", profile_name) is None
        or is_remote
    ):
        raise RuntimeError("MCP child provider profile selection is invalid")
    return profile_name


def _materialization_attestation(cfg: dict[str, _typing.Any]) -> str:
    """Authenticate one complete process-local child declaration.

    The secret-bearing environment and headers must not be separable from the
    executable, transport destination, trust policy, or parent-only controls
    that consume them.  Signing the complete declaration also makes any
    post-materialization mutation fail closed at the child boundary.
    """

    payload = {
        key: value
        for key, value in cfg.items()
        if key != "_runtime_materialization_attestation"
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hmac.new(_SESSION_KEY, canonical, hashlib.sha256).hexdigest()


def _runtime_materialized(cfg: dict[str, _typing.Any]) -> bool:
    """Return whether a runtime-only secret payload was minted in this process."""

    supplied = str(cfg.get("_runtime_materialization_attestation") or "")
    return bool(supplied) and secrets.compare_digest(
        supplied,
        _materialization_attestation(cfg),
    )


def attest_runtime_child_config(cfg: dict[str, _typing.Any]) -> dict[str, _typing.Any]:
    """Mark one in-process child config as safely runtime-materialized.

    Persistent fleet catalogs must carry references rather than credential
    values.  A caller that builds a child config directly from AgentConfig has
    already resolved those references in this process, so it must bind the
    full executable/transport declaration, parent-only controls, sensitive
    field names, and payload to the same per-process attestation used by
    :meth:`MCPMultiplexer.load_catalog` before mounting the child.
    """
    prepared = dict(cfg)
    sensitive_keys = {
        str(key)
        for container_name in ("env", "headers")
        for key in (prepared.get(container_name) or {})
        if _sensitive_config_key(str(key))
    }
    prepared["_runtime_materialized_secret_keys"] = sorted(sensitive_keys)
    prepared["_runtime_materialization_attestation"] = _materialization_attestation(
        prepared
    )
    return prepared


def _load_runtime_child_policy_factory(name: str) -> _typing.Any:
    """Load one unambiguous, installed child-policy factory."""

    try:
        matches = tuple(
            importlib.metadata.entry_points(
                group=_RUNTIME_CHILD_POLICY_GROUP,
                name=name,
            )
        )
    except Exception:
        logger.warning("_load_runtime_child_policy_factory failed", exc_info=True)
        raise RuntimeError("MCP child runtime policy registry is unavailable") from None
    if len(matches) != 1:
        raise RuntimeError("MCP child runtime policy is unavailable")
    try:
        factory = matches[0].load()
    except Exception:
        logger.warning("_load_runtime_child_policy_factory failed", exc_info=True)
        raise RuntimeError("MCP child runtime policy is unavailable") from None
    if not callable(factory):
        raise RuntimeError("MCP child runtime policy is invalid")
    return factory


def _close_runtime_child_policy(policy: _typing.Any) -> None:
    """Close one policy without exposing provider-owned teardown details."""

    try:
        policy.close()
    except Exception:
        logger.error("MCP child runtime policy cleanup failed", exc_info=True)


# The method surface every provider-neutral child policy must implement before
# it is allowed to shape a child's transport.
_RUNTIME_CHILD_POLICY_METHODS = (
    "child_environment",
    "close",
    "fingerprint_catalog",
    "allows_tool",
    "transport_config",
    "verify_before_spawn",
)


def _runtime_child_policy_names(cfg: dict[str, _typing.Any]) -> tuple[str, str]:
    """Validate one child's policy/profile selection and return both names."""

    selected = cfg.get("runtime_policy")
    policy_name = str(selected)
    profile_name = cfg.get("provider_profile")
    if (
        policy_name != selected
        or _RUNTIME_CHILD_POLICY_RE.fullmatch(policy_name) is None
        or not isinstance(profile_name, str)
        or profile_name != profile_name.strip()
        or _RUNTIME_CHILD_POLICY_RE.fullmatch(profile_name) is None
        or any(
            key in cfg
            for key in _RUNTIME_CHILD_POLICY_TRANSPORT_KEYS - {"provider_profile"}
        )
    ):
        raise RuntimeError("MCP child runtime policy selection is invalid")
    return policy_name, profile_name


def _runtime_child_policy_transport(policy: _typing.Any) -> dict[str, _typing.Any]:
    """Verify one materialized policy's method surface and transport config."""

    if not all(
        callable(getattr(policy, method, None))
        for method in _RUNTIME_CHILD_POLICY_METHODS
    ):
        raise RuntimeError("MCP child runtime policy is invalid")
    transport = policy.transport_config()
    if (
        not isinstance(transport, dict)
        or not transport
        or len(transport) > 16
        or not set(transport).issubset(
            _RUNTIME_CHILD_POLICY_TRANSPORT_KEYS - {"provider_profile"}
        )
        or bool(transport.get("command")) == bool(transport.get("url"))
    ):
        raise RuntimeError("MCP child runtime policy transport is invalid")
    return transport


def _runtime_child_policy_config(
    cfg: dict[str, _typing.Any], transport: dict[str, _typing.Any], policy: _typing.Any
) -> dict[str, _typing.Any]:
    """The child config the policy's transport replaces, with the policy attached."""

    prepared = {
        key: value
        for key, value in cfg.items()
        if key not in {"provider_profile", "runtime_policy"}
    }
    prepared.update(transport)
    prepared[_RUNTIME_CHILD_POLICY_INTERNAL_KEY] = policy
    if len(prepared) > 128:
        raise RuntimeError("MCP child runtime policy transport is invalid")
    return prepared


def _prepare_runtime_child_policy(
    cfg: dict[str, _typing.Any], config: RuntimeConfig
) -> tuple[dict[str, _typing.Any], _typing.Any]:
    """Resolve and materialize one optional provider-neutral child policy."""

    if cfg.get("runtime_policy") in (None, ""):
        return cfg, None
    policy_name, profile_name = _runtime_child_policy_names(cfg)

    factory = _load_runtime_child_policy_factory(policy_name)
    policy: _typing.Any = _ABSENT
    try:
        policy = factory(profile_name=profile_name, config=config)
        transport = _runtime_child_policy_transport(policy)
        return _runtime_child_policy_config(cfg, transport, policy), policy
    except Exception:
        logger.warning("_prepare_runtime_child_policy failed", exc_info=True)
        if policy is not _ABSENT:
            _close_runtime_child_policy(policy)
        raise RuntimeError("MCP child runtime policy is unavailable") from None
