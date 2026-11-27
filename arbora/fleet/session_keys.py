"""The session key a fleet call is scoped to: the authenticated session first, the declared local session only when none exists (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import collections.abc as _collections_abc
import hashlib
import logging
import typing as _typing

from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    pass
from arbora.fleet.child_runtime_policy import (
    _SESSION_KEY,
)
from arbora.security.log_redaction import redact_for_log

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


# Well-known ``_meta`` key an in-process caller can set on every ``tools/call``
# it issues (``fastmcp.Client.call_tool(..., meta={LOCAL_SESSION_META_KEY: id})``)
# to explicitly identify which logical session it belongs to. Same wire key as
# ``arbora.observability.correlation.SESSION_HEADER`` so a caller that
# already threads a correlation/session id through ``correlation.inject()`` /
# ``current_carrier()`` gets multiplexer session isolation for free — imported
# lazily below (not at module scope) purely to avoid an eager cross-package
# import at multiplexer load time, not because of any real cycle.
LOCAL_SESSION_META_KEY = "x-session-id"


_MAX_LOCAL_SESSION_ID_BYTES = 256


def _explicit_local_session_key() -> str | None:
    """A caller-declared session id for a NON-HTTP (stdio/in-memory) request.

    CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog — D-W2-6: for stdio/in-memory
    transports the underlying MCP SDK (fastmcp 4.0.0b1 / mcp 2.0.0) gives NO
    stable per-connection identity at all: ``Context.session_id``, the
    low-level ``ServerSession``, and its ``Connection`` are all reconstructed
    fresh on EVERY single request, even within the same open client connection
    (verified empirically — not merely a docstring claim). That is why
    :func:`ambient_session_key`'s non-HTTP branch cannot derive an ambient per-connection
    key the way the HTTP branch does.

    Multiple logically-distinct local callers sharing ONE process (e.g. an
    orchestrator dispatching several concurrent internal task sessions against
    the SAME in-process multiplexer/FastMCP instance) therefore used to
    collapse onto the single ``"__local_stdio__"`` bucket and see each other's
    loaded tools — a real process-global visibility leak, not a hypothetical
    one (reproduced by ``test_per_session_disclosure_isolation`` /
    ``test_list_catalog_mounted_matches_dispatch_reality_across_sessions``).

    The fix is a caller-supplied session id carried in the standard MCP
    request ``_meta`` (``Client.call_tool(..., meta={...})`` is a first-class,
    documented FastMCP mechanism for exactly this kind of contextual data —
    unlike ``Context.session_id`` it is NOT reconstructed per request, it is
    literally provided by the caller on each call). This is deliberately only
    consulted from the non-HTTP branch of :func:`ambient_session_key`: a real HTTP
    session's key is derived from the AUTHENTICATED connection/token, and must
    never be overridable by caller-declared request metadata (that would let
    one HTTP caller simply claim another's session id). Within a single
    trusted process, callers are not adversarial to each other in this way —
    this only ever adds isolation between COOPERATING local callers, it can
    never be used to cross the HTTP trust boundary.
    """
    try:
        from fastmcp.server.dependencies import get_context

        request_context = get_context().request_context
        meta = request_context.meta if request_context is not None else None
    except Exception:
        logger.debug("_explicit_local_session_key failed", exc_info=True)
        return None
    if not isinstance(meta, _collections_abc.Mapping):
        return None
    raw = meta.get(LOCAL_SESSION_META_KEY)
    if not isinstance(raw, str) or not raw:
        return None
    encoded = raw.encode("utf-8")
    if len(encoded) > _MAX_LOCAL_SESSION_ID_BYTES or any(
        ord(character) < 32 for character in raw
    ):
        return None
    digest = hashlib.blake2s(encoded, key=_SESSION_KEY, digest_size=16).hexdigest()
    return f"local_{digest}"


def _context_session_key(get_context: _typing.Any) -> str | None:
    """This request's MCP ``session_id``, when its context carries one."""
    try:
        sid = get_context().session_id
    except RuntimeError as exc:  # deliberate DEBUG: a per-request CONTROL-FLOW probe one rung down the key-source cascade (session_id -> token -> unauthenticated). Absence is the NORMAL case for an unauthenticated caller, not a failure; the cause is preserved and the cascade continues in the caller.
        logger.debug(
            "HTTP context present but no session_id; falling back to token key: %s: %s",
            type(exc).__name__,
            redact_for_log(exc),
        )
        return None
    return str(sid) if sid else None


def _token_session_key(token: _typing.Any) -> str:
    """The stable, keyed per-caller session key derived from one access token."""
    from arbora.api.domain.mcp_server.backing.context_helpers import (
        token_caller_material,
    )

    raw = token_caller_material(token)
    digest = hashlib.blake2s(
        raw.encode("utf-8"), key=_SESSION_KEY, digest_size=16
    ).hexdigest()
    return f"http_{digest}"


def ambient_session_key() -> str:
    """Stable per-connection key for session-scoped tool visibility.

    On a shared streamable-http server every client gets its own
    ``Context.session_id``; with no session context (stdio / single-client) all
    requests fall back to one key so behaviour matches the pre-Phase-5 server —
    UNLESS the caller explicitly declares its own session id (see
    :func:`_explicit_local_session_key`), in which case that takes priority so
    concurrent local callers in one process are not forced to share a bucket.

    The HTTP-context check MUST run before consulting ``Context.session_id``:
    fastmcp 4's ``Context.session_id`` no longer raises when there is no real
    (HTTP) session — for stdio/in-memory transports it silently mints a fresh
    UUID on every single call (no stable ``connection`` to cache it on), which
    would otherwise give every call in the same local session a different key.
    """
    try:
        from fastmcp.server.dependencies import (
            get_access_token,
            get_context,
            get_http_request,
        )

        get_http_request()
    except RuntimeError:
        return _explicit_local_session_key() or "__local_stdio__"
    except ImportError as exc:  # deliberate DEBUG: this is a per-request CONTROL-FLOW probe ("is there an HTTP request context?"), not an error path. Every stdio/local call takes it, so WARNING here would emit one line per request. The cause is preserved (interpolated) and the outcome is encoded in the returned key.
        logger.debug(
            "No HTTP request context; trying next key source: %s: %s",
            type(exc).__name__,
            redact_for_log(exc),
        )
        return _explicit_local_session_key() or "__invalid_http_context__"
    sid = _context_session_key(get_context)
    if sid is not None:
        return sid
    try:
        token = get_access_token()
    except Exception:
        logger.debug("ambient_session_key failed", exc_info=True)
        token = None
    if token is None:
        return "__unauthenticated_http__"
    return _token_session_key(token)
