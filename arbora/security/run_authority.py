"""Run-scoped renewal of a caller's verified credential (ARBORA-IDENTITY-R029).

A caller presents a short-lived bearer (300 s). A run it starts (an agent run,
a fan-out, a long cross-source query) can outlive that bearer. Arbora renews
the caller's authority for the run, not the bearer: the run gets a
:class:`RunCredentialLease` seeded from the verified bearer expiry, and a
renewer extends that in-memory lease before it expires while the run is
active and the caller is not revoked.

Bounds, all fail-closed:

* renewal stops at once when the run ends or is cancelled (the lease is
  revoked, so nothing that escaped the run keeps its authority);
* a known revocation of the caller revokes every live run lease at once;
* renewal never extends a lease past the run's maximum lifetime;
* each renewal extends by at most the issuer's access-token lifetime;
* a lease that already expired never renews.

The lease holds no token or identity material. Audit records carry the
principal, tenant, run reference, event and expiry, never a credential.

This module has no served-runtime imports, so the security layer can use it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from arbora.security.actor_context import (
    ActorContext,
    CredentialExpiredError,
    CredentialLease,
)

logger = logging.getLogger(__name__)
audit_logger = logging.getLogger("arbora.audit.run_authority")

#: Closed vocabulary for run-authority audit events and revocation reasons.
RUN_AUTHORITY_EVENTS = frozenset(
    {
        "started",
        "renewed",
        "revoked",
        "check_failed",
        "lifetime_reached",
        "expired",
        "run_ended",
        "run_cancelled",
    }
)

#: The issuer's access-token ceiling (``arbora.identity.issuer``); a renewal
#: never extends the lease by more than this.
MAX_RENEW_TTL_SECONDS = 300
DEFAULT_MAX_RUN_SECONDS = 4 * 3600


class RunCredentialLease(CredentialLease):
    """A lease over a caller's authority for one run; never process-renewable.

    It is a distinct type so the process-authority renewal path (which
    reacquires the *process* identity) can never touch it.
    """

    def __init__(self, expires_at: int, *, run_deadline: int, run_ref: str) -> None:
        super().__init__(expires_at)
        self.run_deadline = int(run_deadline)
        self.run_ref = run_ref
        self.renewals = 0


@dataclass(frozen=True, slots=True)
class RunRenewalPolicy:
    """Bounds for one run's renewal; the values are received configuration."""

    max_run_seconds: int = DEFAULT_MAX_RUN_SECONDS
    renew_ttl_seconds: int = MAX_RENEW_TTL_SECONDS
    headroom_seconds: int = 30
    poll_seconds: float = 15.0

    def __post_init__(self) -> None:
        if not 1 <= int(self.renew_ttl_seconds) <= MAX_RENEW_TTL_SECONDS:
            raise ValueError("run renewal ttl must be between 1 and 300 seconds")
        if int(self.max_run_seconds) < 1:
            raise ValueError("maximum run lifetime must be positive")
        if not 0 <= int(self.headroom_seconds) < int(self.renew_ttl_seconds):
            raise ValueError("renewal headroom must be shorter than the ttl")
        if float(self.poll_seconds) <= 0:
            raise ValueError("renewal poll interval must be positive")


def run_renewal_policy() -> RunRenewalPolicy:
    """The configured policy (``ARBORA_RUN_MAX_LIFETIME_SECONDS``)."""
    try:
        from arbora.core.settings import setting

        raw = setting("ARBORA_RUN_MAX_LIFETIME_SECONDS", str(DEFAULT_MAX_RUN_SECONDS))
        return RunRenewalPolicy(max_run_seconds=int(raw))
    except Exception:  # noqa: BLE001 — a bad value keeps the bounded default
        logger.warning("Invalid ARBORA_RUN_MAX_LIFETIME_SECONDS; using the default")
        return RunRenewalPolicy()


#: Returns a closed-vocabulary revocation reason for an actor, or ``None``.
RevocationCheck = Callable[[ActorContext], Awaitable[str | None]]


AuditSink = Callable[[dict[str, Any]], None]


def _log_audit(record: dict[str, Any]) -> None:
    audit_logger.info(
        "run_authority event=%s principal=%s tenant=%s run=%s expires_at=%s "
        "renewals=%s reason=%s",
        record["event"],
        record["principal"],
        record["tenant"],
        record["run_ref"],
        record["expires_at"],
        record["renewals"],
        record.get("reason", "-"),
    )


class CallerRevocations:
    """Process registry of caller revocations and live run leases.

    ``revoke_caller`` revokes every live run lease of that caller at once and
    makes later renewal checks refuse. Composition may add further checks
    (for example the engine's session facts) with :meth:`add_check`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revoked: dict[tuple[str, str], str] = {}
        self._leases: dict[tuple[str, str], set[RunCredentialLease]] = {}
        self._checks: list[RevocationCheck] = []

    @staticmethod
    def _key(actor: ActorContext) -> tuple[str, str]:
        return (str(actor.tenant_id), str(actor.actor_id))

    def add_check(self, check: RevocationCheck) -> None:
        with self._lock:
            self._checks.append(check)

    def clear(self) -> None:
        with self._lock:
            self._revoked.clear()
            self._leases.clear()
            self._checks.clear()

    def register(self, actor: ActorContext, lease: RunCredentialLease) -> None:
        with self._lock:
            reason = self._revoked.get(self._key(actor))
            self._leases.setdefault(self._key(actor), set()).add(lease)
        if reason is not None:
            lease.revoke(reason)

    def unregister(self, actor: ActorContext, lease: RunCredentialLease) -> None:
        with self._lock:
            leases = self._leases.get(self._key(actor))
            if leases is not None:
                leases.discard(lease)
                if not leases:
                    del self._leases[self._key(actor)]

    def revoke_caller(self, tenant: str, principal: str, reason: str) -> int:
        """Revoke a caller now; returns how many live run leases closed."""
        key = (str(tenant), str(principal))
        with self._lock:
            self._revoked.setdefault(key, reason)
            leases = tuple(self._leases.get(key, ()))
        for lease in leases:
            lease.revoke(reason)
        return len(leases)

    def restore_caller(self, tenant: str, principal: str) -> None:
        """Forget a revocation (a new sign-in); live revoked leases stay closed."""
        with self._lock:
            self._revoked.pop((str(tenant), str(principal)), None)

    async def __call__(self, actor: ActorContext) -> str | None:
        with self._lock:
            reason = self._revoked.get(self._key(actor))
            checks = tuple(self._checks)
        if reason is not None:
            return reason
        for check in checks:
            reason = await check(actor)
            if reason is not None:
                return reason
        return None


CALLER_REVOCATIONS = CallerRevocations()


def caller_run_renewable(actor: Any) -> bool:
    """True for a verified caller credential with a bounded expiry and no lease."""
    return (
        isinstance(actor, ActorContext)
        and actor.authenticated
        and actor.credential_lease is None
        and actor.credential_expires_at is not None
    )


def open_run_lease(
    actor: ActorContext,
    *,
    policy: RunRenewalPolicy,
    clock: Callable[[], float] | None = None,
) -> RunCredentialLease:
    """Seed a run lease from the caller's verified expiry; never extend it here."""
    clock = clock or time.time
    expires_at = int(actor.credential_expires_at or 0)
    now = int(clock())
    if now >= expires_at:
        raise CredentialExpiredError("Verified actor credential has expired")
    return RunCredentialLease(
        expires_at,
        run_deadline=now + int(policy.max_run_seconds),
        run_ref=secrets.token_hex(8),
    )


def audit_record(
    event: str,
    actor: ActorContext,
    lease: RunCredentialLease,
    reason: str | None = None,
) -> dict[str, Any]:
    if event not in RUN_AUTHORITY_EVENTS:
        raise ValueError(f"unknown run authority event {event!r}")
    record: dict[str, Any] = {
        "event": event,
        "principal": str(actor.actor_id),
        "tenant": str(actor.tenant_id),
        "run_ref": lease.run_ref,
        "expires_at": lease.expires_at,
        "renewals": lease.renewals,
    }
    if reason is not None:
        record["reason"] = reason
    return record


async def keep_run_authority_current(
    actor: ActorContext,
    lease: RunCredentialLease,
    *,
    policy: RunRenewalPolicy,
    revocation_check: RevocationCheck = CALLER_REVOCATIONS,
    audit: AuditSink = _log_audit,
    clock: Callable[[], float] | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> int:
    """Renew ``lease`` before each expiry until a stop condition; return renewals.

    The caller cancels this coroutine when the run ends. A revocation, the
    maximum lifetime or an already-expired lease ends it on its own.
    """
    clock = clock or time.time
    while True:
        if lease.revocation_reason is not None:
            audit(audit_record("revoked", actor, lease, lease.revocation_reason))
            return lease.renewals
        try:
            reason = await revocation_check(actor)
        except Exception as exc:  # noqa: BLE001 — fail closed: no renewal this tick
            audit(audit_record("check_failed", actor, lease, type(exc).__name__))
            reason = None
            checked = False
        else:
            checked = True
        if reason is not None:
            lease.revoke(reason)
            audit(audit_record("revoked", actor, lease, reason))
            return lease.renewals
        now = clock()
        if now >= lease.expires_at:
            audit(audit_record("expired", actor, lease))
            return lease.renewals
        due = lease.expires_at - policy.headroom_seconds
        if checked and now >= due:
            target = min(int(now) + policy.renew_ttl_seconds, lease.run_deadline)
            if target <= lease.expires_at:
                audit(audit_record("lifetime_reached", actor, lease))
                return lease.renewals
            try:
                lease.renew(target)
            except CredentialExpiredError:
                audit(audit_record("expired", actor, lease))
                return lease.renewals
            lease.renewals += 1
            audit(audit_record("renewed", actor, lease))
            continue
        wait = max(0.0, min(float(policy.poll_seconds), due - now))
        await sleep(wait if wait > 0 else min(1.0, float(policy.poll_seconds)))
