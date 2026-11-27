"""Fleet capability scopes: which scopes admit discover, delegate and manage callers (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import logging
import typing as _typing

import fastmcp.exceptions as _fastmcp_exceptions

from arbora.core.settings import RuntimeConfig
from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    pass

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


def _stdio_capabilities() -> frozenset[str]:
    """Capabilities for a local stdio caller: the process's own verified
    actor context (the same context the serving host binds for every stdio
    tool call), read through its already-resolved effective capability set
    (``roles`` — role ∪ scope ∪ group-derived capabilities). An unbound or
    unauthenticated actor is refused exactly like an unauthenticated remote
    caller — stdio carries no implicit trust.
    """
    from arbora.security.actor_context import current_actor

    try:
        actor = current_actor()
    except Exception:
        logger.warning("_stdio_capabilities failed", exc_info=True)
        raise _fastmcp_exceptions.ToolError(
            "Authenticated local process context required"
        ) from None
    if actor.authenticated is not True:
        raise _fastmcp_exceptions.ToolError(
            "Authenticated local process context required"
        ) from None
    return frozenset(str(role).strip() for role in actor.roles if str(role).strip())


def _http_capabilities(token: _typing.Any, config: RuntimeConfig) -> frozenset[str]:
    """Capabilities for a verified HTTP bearer token: its own declared
    scopes plus any identity-group-derived capabilities its claims map to.
    """
    capabilities = {
        str(scope).strip()
        for scope in (getattr(token, "scopes", None) or [])
        if str(scope).strip()
    }
    claims = getattr(token, "claims", None)
    if isinstance(claims, dict):
        try:
            import arbora.security.identity as _identity

            capabilities.update(
                _identity.base_capabilities(
                    _identity.normalize_identity(claims),
                    config.identity_group_capability_map,
                )
            )
        except Exception:
            logger.warning("_http_capabilities failed", exc_info=True)
            raise _fastmcp_exceptions.ToolError(
                "Verified capability mapping unavailable"
            ) from None
    return frozenset(capabilities)


def _request_capabilities(config: RuntimeConfig) -> frozenset[str]:
    """Return the verified caller's capabilities for every transport.

    Either path (stdio or HTTP) must resolve to a concrete, authenticated
    capability set: there is no longer a transport that bypasses this check.
    """
    from arbora.security.anonymous import admitted_actor
    from arbora.security.session import current_session

    session = current_session()
    if session is not None and admitted_actor(session.actor):
        session.ensure_authority_current()
        return frozenset(session.scopes)
    try:
        from fastmcp.server.dependencies import get_access_token, get_http_request

        get_http_request()
    except RuntimeError:
        return _stdio_capabilities()
    except Exception:
        logger.warning("_request_capabilities failed", exc_info=True)
        raise _fastmcp_exceptions.ToolError(
            "Authenticated HTTP context required"
        ) from None
    token = get_access_token()
    if token is None:
        raise _fastmcp_exceptions.ToolError("Authenticated HTTP context required")
    return _http_capabilities(token, config)


def fleet_required_capabilities(kind: str) -> set[str]:
    """Capability alternatives for one bounded fleet operation kind.

    ``manage`` has no narrower fleet-specific scope, so an administrative
    grant is its exact requirement. ``discover`` and ``delegate`` each have
    their own dedicated fleet scope; a generic ``admin``/``kg:admin``/
    ``mcp:admin`` grant is a different capability entirely and is never
    accepted in its place — the exact fleet scope is always required.
    """
    administrative = {"admin", "kg:admin", "mcp:admin"}
    return {
        "discover": {"mcp:discover", "mcp:delegate"},
        "manage": administrative,
        "delegate": {"mcp:delegate"},
    }.get(kind, {"mcp:delegate"})


def require_fleet_capability(
    kind: str, config: RuntimeConfig, extra_scopes: list[str] | None = None
) -> None:
    """Authorize fleet discovery/delegation for every transport, local stdio
    included: the exact same capability check applies regardless of how the
    caller connected."""
    capabilities = _request_capabilities(config)
    required = fleet_required_capabilities(kind)
    if not capabilities.intersection(required):
        raise _fastmcp_exceptions.ToolError(f"MCP fleet {kind} capability required")
    if extra_scopes and not set(extra_scopes).issubset(capabilities):
        raise _fastmcp_exceptions.ToolError("Child MCP capability scope required")
