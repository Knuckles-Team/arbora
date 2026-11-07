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

    ANONYMOUS = "anonymous"
    HUMAN = "human"
    AI_AGENT = "ai_agent"
    AUTOMATED_SERVICE = "automated_service"
    HYBRID_TEAM = "hybrid_team"
    SYSTEM = "system"


class CredentialExpiredError(PermissionError):
    """Raised when an authenticated actor's validated credential has expired."""


class LeaseState(StrEnum):
    """The closed states of a credential lease (ARBORA-IDENTITY-R027.1)."""

    LIVE = "live"
    RENEW_DUE = "renew_due"
    RENEWING = "renewing"
    DEGRADED = "degraded"
    EXPIRED = "expired"
    REVOKED = "revoked"
    STOPPED = "stopped"


class LeaseEvent(StrEnum):
    """The closed events a credential lease accepts (ARBORA-IDENTITY-R027.1)."""

    TICK = "tick"
    RENEW_START = "renew_start"
    IDP_OK = "idp_ok"
    IDP_ERR = "idp_err"
    IDENTITY_CHANGED = "identity_changed"
    REVOKE = "revoke"
    STOP = "stop"


class LeaseGuard(StrEnum):
    """The closed guards of the credential lease machine."""

    AT_HALF_LIFETIME = "at_half_lifetime"
    BEFORE_EXPIRY = "before_expiry"
    AT_EXPIRY = "at_expiry"
    IDENTITY_UNCHANGED = "identity_unchanged"


#: States no event leaves.
TERMINAL_LEASE_STATES = frozenset({LeaseState.REVOKED, LeaseState.STOPPED})

#: The wire code of :class:`IllegalLeaseTransition`.
LEASE_TRANSITION_ILLEGAL = "LEASE_TRANSITION_ILLEGAL"
LEASE_MACHINE = "credential_lease"


@dataclass(frozen=True, slots=True)
class LeaseEdge:
    """One legal edge: ``source`` moves to ``target`` on ``event`` if ``guard``."""

    source: LeaseState
    event: LeaseEvent
    target: LeaseState
    guard: LeaseGuard | None = None

    @property
    def edge_id(self) -> str:
        """``<from>-<event>-<to>``, the id the bound tests use."""
        return "-".join((_short(self.source), str(self.event), _short(self.target)))


def _short(state: LeaseState) -> str:
    return state.value.replace("_", "")


_S = LeaseState
_E = LeaseEvent
_G = LeaseGuard
_RENEWABLE = (_S.LIVE, _S.RENEW_DUE, _S.RENEWING, _S.DEGRADED, _S.EXPIRED)

#: The 26 legal edges of ARBORA-IDENTITY-R027.1, in table order. Where one
#: (state, event) pair has several edges, the first edge whose guard holds wins.
LEASE_EDGES: tuple[LeaseEdge, ...] = (
    # (10) before (1): an expired Live lease skips RenewDue.
    LeaseEdge(_S.LIVE, _E.TICK, _S.EXPIRED, _G.AT_EXPIRY),
    LeaseEdge(_S.LIVE, _E.TICK, _S.RENEW_DUE, _G.AT_HALF_LIFETIME),
    LeaseEdge(_S.RENEW_DUE, _E.RENEW_START, _S.RENEWING),
    LeaseEdge(_S.RENEWING, _E.IDP_OK, _S.LIVE, _G.IDENTITY_UNCHANGED),
    LeaseEdge(_S.RENEWING, _E.IDP_ERR, _S.DEGRADED, _G.BEFORE_EXPIRY),
    LeaseEdge(_S.RENEWING, _E.IDP_ERR, _S.EXPIRED, _G.AT_EXPIRY),
    LeaseEdge(_S.DEGRADED, _E.IDP_ERR, _S.DEGRADED),
    LeaseEdge(_S.DEGRADED, _E.IDP_OK, _S.LIVE, _G.IDENTITY_UNCHANGED),
    LeaseEdge(_S.EXPIRED, _E.IDP_ERR, _S.EXPIRED),
    LeaseEdge(_S.EXPIRED, _E.IDP_OK, _S.LIVE, _G.IDENTITY_UNCHANGED),
    LeaseEdge(_S.RENEW_DUE, _E.TICK, _S.EXPIRED, _G.AT_EXPIRY),
    LeaseEdge(_S.RENEWING, _E.TICK, _S.EXPIRED, _G.AT_EXPIRY),
    LeaseEdge(_S.DEGRADED, _E.TICK, _S.EXPIRED, _G.AT_EXPIRY),
    *(
        LeaseEdge(state, _E.IDENTITY_CHANGED, _S.REVOKED)
        for state in (_S.RENEWING, _S.DEGRADED, _S.EXPIRED)
    ),
    *(LeaseEdge(state, _E.REVOKE, _S.REVOKED) for state in _RENEWABLE),
    *(LeaseEdge(state, _E.STOP, _S.STOPPED) for state in _RENEWABLE),
)

_EDGES_BY_PAIR: dict[tuple[LeaseState, LeaseEvent], tuple[LeaseEdge, ...]] = {}
for _edge in LEASE_EDGES:
    _EDGES_BY_PAIR[(_edge.source, _edge.event)] = (
        *_EDGES_BY_PAIR.get((_edge.source, _edge.event), ()),
        _edge,
    )
del _edge


class IllegalLeaseTransition(CredentialExpiredError):
    """A credential lease refused an event (``LEASE_TRANSITION_ILLEGAL``).

    It subclasses :class:`CredentialExpiredError` so every caller that already
    fails closed on an unusable credential keeps failing closed.
    """

    code = LEASE_TRANSITION_ILLEGAL
    machine = LEASE_MACHINE

    def __init__(self, from_state: LeaseState, event: LeaseEvent) -> None:
        super().__init__(
            f"credential lease refuses {event.value!r} in state {from_state.value!r}"
        )
        self.from_state = from_state
        self.event = event


@dataclass(frozen=True, slots=True)
class LeaseFacts:
    """What the guards read: the lease window and the minted identity verdict."""

    issued_at: float
    expires_at: float
    renew_due_at: float
    identity_unchanged: bool = True

    def holds(self, guard: LeaseGuard | None, now: float) -> bool:
        if guard is None:
            return True
        if guard is LeaseGuard.AT_HALF_LIFETIME:
            return now >= self.renew_due_at
        if guard is LeaseGuard.BEFORE_EXPIRY:
            return now < self.expires_at
        if guard is LeaseGuard.AT_EXPIRY:
            return now >= self.expires_at
        return self.identity_unchanged


def transition(
    lease_state: LeaseState, event: LeaseEvent, now: float, *, facts: LeaseFacts
) -> LeaseState:
    """The one transition function of the credential lease machine.

    Returns the next state, or raises :class:`IllegalLeaseTransition` for a
    pair outside :data:`LEASE_EDGES` or a legal pair whose guards all fail.
    """
    for edge in _EDGES_BY_PAIR.get((lease_state, event), ()):
        if facts.holds(edge.guard, now):
            return edge.target
    raise IllegalLeaseTransition(lease_state, event)


class CredentialLease:
    """Thread-safe in-memory credential lease shared by one renewable identity.

    The lease contains no token or identity material. Long-lived worker threads
    retain the same verified :class:`ActorContext` and consult this lease at
    every graph boundary, so a successful OAuth/token rotation renews all of
    them atomically while a failed rotation remains fail-closed.

    The lease is an explicit state machine (ARBORA-IDENTITY-R027.1): ``state``
    is its only state field and :meth:`apply` (through :func:`transition`) is
    its only writer. The expiry, attempt count, failure code and revocation
    reason are data. The lease lives in memory, so a restart begins at ``Live``.
    """

    def __init__(self, expires_at: int, *, issued_at: float | None = None) -> None:
        self._lock = threading.RLock()
        self.state: LeaseState = LeaseState.LIVE
        self._issued_at = float(time.time() if issued_at is None else issued_at)
        self._expires_at = int(expires_at)
        self._attempts = 0
        self._failure_code: str | None = None
        self._revocation_reason: str | None = None

    def _renew_due_at(self, issued_at: float, expires_at: float) -> float:
        """Half of the token lifetime (the ``at_half_lifetime`` guard)."""
        return issued_at + max(0.0, expires_at - issued_at) / 2.0

    def _facts(self, identity_unchanged: bool) -> LeaseFacts:
        return LeaseFacts(
            issued_at=self._issued_at,
            expires_at=float(self._expires_at),
            renew_due_at=self._renew_due_at(self._issued_at, self._expires_at),
            identity_unchanged=identity_unchanged,
        )

    @property
    def expires_at(self) -> int:
        with self._lock:
            return self._expires_at

    @property
    def renew_due_at(self) -> float:
        with self._lock:
            return self._renew_due_at(self._issued_at, self._expires_at)

    @property
    def attempts(self) -> int:
        with self._lock:
            return self._attempts

    @property
    def failure_code(self) -> str | None:
        with self._lock:
            return self._failure_code

    @property
    def revocation_reason(self) -> str | None:
        with self._lock:
            return self._revocation_reason

    def apply(
        self,
        event: LeaseEvent,
        now: float | None = None,
        *,
        identity_unchanged: bool = True,
        expires_at: int | None = None,
        code: str | None = None,
    ) -> LeaseState:
        """Apply one event; the only path that changes ``state``.

        ``expires_at`` is the minted expiry an ``idp_ok`` carries; ``code`` is
        the failure code of ``idp_err`` or the reason of ``identity_changed``
        and ``revoke``. A refusal raises and leaves the lease unchanged.
        """
        with self._lock:
            moment = time.time() if now is None else float(now)
            if event is LeaseEvent.IDP_OK and expires_at is None:
                raise ValueError("idp_ok carries the minted expiry")
            target = transition(
                self.state, event, moment, facts=self._facts(identity_unchanged)
            )
            if event is LeaseEvent.IDP_OK:
                self._issued_at = moment
                self._expires_at = int(expires_at or 0)
                self._attempts = 0
                self._failure_code = None
            elif event is LeaseEvent.IDP_ERR:
                self._attempts += 1
                self._failure_code = code or "renewal_failed"
            elif target is LeaseState.REVOKED:
                self._revocation_reason = code or str(event)
            self.state = target
            return target

    def tick(self, now: float | None = None) -> LeaseState:
        """Send ``tick`` when one of its guards holds; otherwise change nothing."""
        with self._lock:
            moment = time.time() if now is None else float(now)
            edges = _EDGES_BY_PAIR.get((self.state, LeaseEvent.TICK), ())
            facts = self._facts(True)
            if any(facts.holds(edge.guard, moment) for edge in edges):
                return self.apply(LeaseEvent.TICK, moment)
            return self.state

    def revoke(self, reason: str) -> None:
        """Permanently close this lease after verified authority loss."""
        with self._lock:
            if self.state not in TERMINAL_LEASE_STATES:
                self.apply(LeaseEvent.REVOKE, code=reason)

    def stop(self) -> None:
        """Close this lease when its owner stops; a terminal lease is left as is."""
        with self._lock:
            if self.state not in TERMINAL_LEASE_STATES:
                self.apply(LeaseEvent.STOP)

    def ensure_current(self) -> None:
        """Fail closed unless the lease is usable now (request-time check)."""
        with self._lock:
            if self.state is LeaseState.REVOKED:
                raise CredentialExpiredError("Verified actor credential was revoked")
            if self.state is LeaseState.STOPPED:
                raise CredentialExpiredError("Verified actor credential is closed")
            self.tick()
            if self.state is LeaseState.EXPIRED:
                raise CredentialExpiredError("Verified actor credential has expired")

    def renew(self, expires_at: int, now: float | None = None) -> None:
        """Commit a freshly minted, identity-checked expiry through the machine.

        A ``Live`` or ``RenewDue`` lease passes through ``RenewDue`` and
        ``Renewing``; a ``Degraded`` or ``Expired`` lease recovers directly
        (ARBORA-IDENTITY-R027.3). ``Revoked`` and ``Stopped`` refuse.
        """
        with self._lock:
            moment = time.time() if now is None else float(now)
            if int(expires_at) <= moment:
                raise CredentialExpiredError("A renewed credential must outlive now")
            self.tick(moment)
            if self.state is LeaseState.LIVE:
                self.apply(LeaseEvent.TICK, moment)
            if self.state is LeaseState.RENEW_DUE:
                self.apply(LeaseEvent.RENEW_START, moment)
            self.apply(LeaseEvent.IDP_OK, moment, expires_at=int(expires_at))


@dataclass(frozen=True)
class ActorContext:
    """The identity of whoever is currently reading or writing the graph.

    ``roles`` drives role-based ACL checks; ``tenant_id`` drives multi-tenant
    isolation.

    ``authenticated`` is True only when the identity was minted server-side
    from a validated credential (JWT via the gateway middleware or a validated
    configured process credential). Caller-supplied identities cannot authorize
    graph access. Explicit local anonymous admission is separately represented
    by ``authority_mode=none`` and never sets ``authenticated=True``.
    """

    actor_id: str = "system"
    actor_type: ActorType = ActorType.SYSTEM
    roles: tuple[str, ...] = field(default_factory=tuple)
    tenant_id: str = ""
    authenticated: bool = False
    authority_mode: str = "verified"
    # Raw IdP group memberships (Okta ``groups`` / Keycloak group mapper),
    # provider-normalized. ``roles`` is the effective *capability* set (roles ∪
    # scopes ∪ group-derived capabilities) that ACL checks read; ``groups`` is
    # retained distinctly for identity propagation that needs the group names
    # themselves — notably k8s ``Impersonate-Group``
    # (CONCEPT:MERISTEM-OS.identity.idp-agnostic-role-inheritance).
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


@contextmanager
def suspend_actor() -> Iterator[None]:
    """Remove ambient identity at an explicitly anonymous boundary."""
    token = _current.set(None)
    try:
        yield None
    finally:
        _current.reset(token)
