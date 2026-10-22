"""Project supplied, request-bound verified session authority into B's caller."""

from __future__ import annotations

import contextlib
import contextvars
import logging
from collections.abc import Callable, Iterator, Mapping
from typing import TYPE_CHECKING, Any

from arbora.security.anonymous import session_context

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from arbora.api.invoke import VerifiedCaller


def caller_from_session(
    session: Any,
    *,
    credential_kind: str,
    request_id: str = "",
    mfa_at_ms: int | None = None,
) -> VerifiedCaller:
    """Copy verified facts without deriving grants or classifying unknown actors.

    The composition root must supply a session resolved by the verified authority
    for this request. This function does not authenticate client-provided objects.
    Credential kind is supplied by that authority, never inferred from actor kind.
    """
    from arbora.api.invoke import VerifiedCaller
    from arbora.api.invoke.steps import AnonymousCaller
    from arbora.security.anonymous import admitted_actor

    _require_session_ports(session, credential_kind)
    session.ensure_authority_current()
    actor = session.actor
    actor.ensure_credential_current()
    principal_kind = _PRINCIPAL_KINDS.get(str(actor.actor_type or ""))
    anonymous = admitted_actor(actor)
    if (actor.authenticated is not True and not anonymous) or principal_kind is None:
        raise PermissionError("Verified principal kind required")
    claims = session_context(session)
    scopes = session.scopes
    if not _scopes_valid(scopes, claims) or not _claims_match(claims, session, actor):
        raise PermissionError("Verified session authority mismatch")
    if mfa_at_ms is not None and type(mfa_at_ms) is not int:
        raise PermissionError("Verified step-up authority required")
    caller_type = AnonymousCaller if anonymous else VerifiedCaller
    return caller_type(
        principal=actor.actor_id,
        tenant=session.tenant,
        effective_scopes=frozenset(scopes),
        engine_claims=claims,
        principal_kind=principal_kind,
        authenticated=not anonymous,
        delegated=bool(claims["delegation"]),
        credential_kind="none" if anonymous else credential_kind,
        policy_revision=session.policy_version,
        request_id=request_id,
        mfa_at_ms=mfa_at_ms,
        session=session,
    )


#: Verified actor type -> caller principal kind. The served boundary mints a
#: client-credentials (no-email) identity as ``automated_service``
#: (``arbora.security.request_identity``); it is a service principal. Any other
#: kind is never inferred and refuses.
_PRINCIPAL_KINDS: dict[str, str] = {
    "anonymous": "anonymous",
    "human": "human",
    "service": "service",
    "automated_service": "service",
}

_SCOPE_TYPES = (tuple, list, set, frozenset)


def _require_session_ports(session: Any, credential_kind: Any) -> None:
    """Refuse a missing session, credential kind or verified-authority port."""
    if session is None or not isinstance(credential_kind, str) or not credential_kind:
        raise PermissionError("Verified session authority required")
    ports = ("ensure_authority_current", "engine_verified_context")
    if not all(callable(getattr(session, name, None)) for name in ports):
        raise PermissionError("Current verified engine authority required")


def _scopes_valid(scopes: Any, claims: Any) -> bool:
    """Return True when session scopes are concrete and equal the claimed scopes."""
    if not isinstance(claims, Mapping) or not isinstance(scopes, _SCOPE_TYPES):
        return False
    if any(not isinstance(scope, str) or not scope or "*" in scope for scope in scopes):
        return False
    claimed = claims.get("scopes")
    if not isinstance(claimed, _SCOPE_TYPES):
        return False
    if any(not isinstance(scope, str) for scope in claimed):
        return False
    return frozenset(claimed) == frozenset(scopes)


def _claims_match(claims: Mapping[str, Any], session: Any, actor: Any) -> bool:
    """Return True when engine claims bind this exact actor, tenant and policy."""
    identity = (actor.actor_id, session.tenant, session.policy_version)
    if any(not isinstance(value, str) or not value.strip() for value in identity):
        return False
    return (
        claims.get("principal") == actor.actor_id
        and claims.get("tenant") == session.tenant
        and actor.tenant_id == session.tenant
        and claims.get("policy_version") == session.policy_version
        and isinstance(claims.get("delegation"), (tuple, list))
    )


_REQUEST_REF: contextvars.ContextVar[str] = contextvars.ContextVar(
    "arbora_request_ref", default=""
)


@contextlib.contextmanager
def use_request_ref(ref: str) -> Iterator[None]:
    """Name the current in-process request (an agent run's tool call)."""
    token = _REQUEST_REF.set(str(ref)[:256])
    try:
        yield
    finally:
        _REQUEST_REF.reset(token)


def mcp_request_ref() -> str:
    """The current request's reference, or empty outside a request.

    An in-process call bound by :func:`use_request_ref` names itself. An MCP
    request is ``mcp:<session id>:<JSON-RPC request id>``: JSON-RPC ids
    restart per session, so the session id keeps two sessions apart.
    """
    bound = _REQUEST_REF.get()
    if bound:
        return bound
    try:
        from fastmcp.server.dependencies import get_context

        ctx = get_context()
        request_id = ctx.request_id
        session_id = ctx.session_id
    except Exception:  # noqa: BLE001 - no MCP request in scope
        return ""
    if not request_id:
        return ""
    return f"mcp:{session_id or '-'}:{request_id}"[:256]


class VerifiedMCPCaller:
    """Synchronous callback compatible with ``MCPProjection``.

    Both resolvers are mandatory, server-owned, and request scoped. They must
    resolve the same currently verified credential; a process-wide identity is
    not a fallback for an unauthenticated remote request. A stdio process session
    must be explicitly bound by its verified authority before calling this port.
    """

    def __init__(
        self,
        *,
        session_for_request: Callable[[], Any],
        credential_kind_for_request: Callable[[], str],
    ) -> None:
        if not callable(session_for_request) or not callable(
            credential_kind_for_request
        ):
            raise ValueError("Verified MCP authority resolvers required")
        self._session_for_request = session_for_request
        self._credential_kind_for_request = credential_kind_for_request

    def caller_for_request(self) -> VerifiedCaller | None:
        """Resolve afresh on every discovery or invocation; denial has no caller."""
        try:
            return caller_from_session(
                self._session_for_request(),
                credential_kind=self._credential_kind_for_request(),
                request_id=mcp_request_ref(),
            )
        except PermissionError as exc:
            # Static, non-secret reason, so a refused caller is diagnosable.
            logger.warning("MCP caller refused: %s", exc)
            return None
