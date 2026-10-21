"""Verified ambient actor context for served arbora requests.

The trust, permission, and audit layers need to know *who* is reading or
writing. Threading an actor through hundreds of existing call sites
would be invasive, so identity is carried in a ``contextvars.ContextVar``.
Served boundaries set it with :func:`use_actor`. No identity is synthesized:
an operation without verified authority fails closed.

This module has no arbora imports, so any layer can import it without cycles.
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum


class ActorType(StrEnum):
    """Provenance classification for a verified actor; it never grants access."""

    HUMAN = "human"
    AI_AGENT = "ai_agent"
    AUTOMATED_SERVICE = "automated_service"
    HYBRID_TEAM = "hybrid_team"
    SYSTEM = "system"


class CredentialExpiredError(PermissionError):
    """Raised when an authenticated actor's validated credential has expired."""


class CredentialLease:
    """Thread-safe in-memory expiry shared by one renewable process identity.

    The lease contains no token or identity material. Long-lived worker threads
    retain the same verified :class:`ActorContext` and consult this mutable
    expiry at every graph boundary, so a successful OAuth/token rotation renews
    all of them atomically while a failed rotation remains fail-closed.
    """

    def __init__(self, expires_at: int) -> None:
        self._lock = threading.Lock()
        self._expires_at = int(expires_at)
        self._revocation_reason: str | None = None

    @property
    def expires_at(self) -> int:
        with self._lock:
            return self._expires_at

    @property
    def revocation_reason(self) -> str | None:
        with self._lock:
            return self._revocation_reason

    def revoke(self, reason: str) -> None:
        """Permanently close this lease after verified authority loss."""
        with self._lock:
            if self._revocation_reason is None:
                self._revocation_reason = reason

    def _ensure_current_locked(self) -> None:
        if self._revocation_reason is not None:
            raise CredentialExpiredError("Verified actor credential was revoked")
        if int(time.time()) >= self._expires_at:
            raise CredentialExpiredError("Verified actor credential has expired")

    def ensure_current(self) -> None:
        with self._lock:
            self._ensure_current_locked()

    def renew(self, expires_at: int) -> None:
        """Update a still-live lease atomically; never resurrect old authority."""
        with self._lock:
            self._ensure_current_locked()
            self._expires_at = int(expires_at)


@dataclass(frozen=True)
class ActorContext:
    """The identity of whoever is currently reading or writing the graph.

    ``roles`` drives role-based ACL checks; ``tenant_id`` drives multi-tenant
    isolation.

    ``authenticated`` is True only when the identity was minted server-side
    from a validated credential (JWT via the gateway middleware or a validated
    configured process credential). Caller-supplied identities remain
    unauthenticated and therefore cannot authorize graph access.
    """

    actor_id: str = "system"
    actor_type: ActorType = ActorType.SYSTEM
    roles: tuple[str, ...] = field(default_factory=tuple)
    tenant_id: str = ""
    authenticated: bool = False
    # Raw IdP group memberships (Okta ``groups`` / Keycloak group mapper),
    # provider-normalized. ``roles`` is the effective *capability* set (roles ∪
    # scopes ∪ group-derived capabilities) that ACL checks read; ``groups`` is
    # retained distinctly for identity propagation that needs the group names
    # themselves — notably k8s ``Impersonate-Group``
    # (CONCEPT:AU-OS.identity.idp-agnostic-role-inheritance).
    groups: tuple[str, ...] = field(default_factory=tuple)
    # Absolute UNIX expiry from an already-validated bearer credential. It is
    # runtime-only authority state so a short-lived JWT cannot become an
    # indefinite process session. Non-token/bootstrap actors leave it unset.
    credential_expires_at: int | None = None
    credential_lease: CredentialLease | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def ensure_credential_current(self) -> None:
        """Fail closed when this actor's validated credential is no longer live."""
        if self.credential_lease is not None:
            self.credential_lease.ensure_current()
            return

        expiry = self.credential_expires_at
        if expiry is None:
            return
        try:
            expired = int(time.time()) >= int(expiry)
        except (TypeError, ValueError):
            raise CredentialExpiredError(
                "Verified actor credential has an invalid expiry"
            ) from None
        if expired:
            raise CredentialExpiredError("Verified actor credential has expired")


class IdentityRequiredError(PermissionError):
    """Raised when an operation has no explicitly bound actor identity."""


_current: contextvars.ContextVar[ActorContext | None] = contextvars.ContextVar(
    "company_brain_actor", default=None
)


def current_actor() -> ActorContext:
    """Return the explicitly bound actor or fail closed."""
    actor = _current.get()
    if actor is None:
        raise IdentityRequiredError("A verified actor context is required")
    actor.ensure_credential_current()
    return actor


def set_actor(ctx: ActorContext) -> contextvars.Token[ActorContext | None]:
    """Set the current actor, returning a token for :func:`reset_actor`."""
    ctx.ensure_credential_current()
    return _current.set(ctx)


def reset_actor(token: contextvars.Token[ActorContext | None]) -> None:
    """Restore the actor to its prior value."""
    _current.reset(token)


# ---------------------------------------------------------------------------
# Source-system context — "which system did this write come from?"
# ---------------------------------------------------------------------------
# Distinct from the actor ("who"): a single agent may ingest from ServiceNow
# then ERPNext in one run. Ingestion paths set this so the write-path guard can
# resolve source authority/trust. Defaults to "" (falls back to the actor id).
_source: contextvars.ContextVar[str] = contextvars.ContextVar(
    "company_brain_source", default=""
)


def current_source() -> str:
    """Return the source-system label for the current write context."""
    return _source.get()


@contextmanager
def use_source(source_system: str) -> Iterator[str]:
    """Scope writes to a named source system (for trust/provenance)."""
    token = _source.set(source_system or "")
    try:
        yield source_system
    finally:
        _source.reset(token)


@contextmanager
def use_actor(ctx: ActorContext) -> Iterator[ActorContext]:
    """Scope a block of work to ``ctx`` (restores the previous actor on exit).

    Example::

        with use_actor(ActorContext("agent:marketing", ActorType.AI_AGENT,
                                    roles=("marketing",), tenant_id="acme")):
            kg.search("...")  # reads filtered to what marketing may see
    """
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
