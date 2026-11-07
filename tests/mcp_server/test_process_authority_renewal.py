"""The process authority renews indefinitely and reports why when it cannot."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from arbora.api.contracts.session import GraphSession, SessionExpiredError
from arbora.mcp_server import bootstrap
from arbora.observability import runtime_health
from arbora.security.actor_context import (
    ActorContext,
    ActorType,
    CredentialLease,
    LeaseState,
)

ROLES = ("kg:admin", "mcp:discover", "query:unified")


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    fake = Clock()
    monkeypatch.setattr(time, "time", fake.time)
    monkeypatch.setattr(bootstrap, "_PROCESS_SESSION_REFRESH_LOCK", threading.Lock())
    yield fake
    runtime_health.register_process_lease(None)


def _actor(clock: Clock, roles: tuple[str, ...] = ROLES) -> ActorContext:
    return ActorContext(
        actor_id="service:arbora",
        actor_type=ActorType.AUTOMATED_SERVICE,
        roles=roles,
        tenant_id="homelab",
        authenticated=True,
        credential_expires_at=int(clock.now) + 300,
    )


def _session(clock: Clock) -> GraphSession:
    actor = _actor(clock)
    actor = replace(actor, credential_lease=CredentialLease(int(clock.now) + 300))
    return GraphSession(
        actor=actor,
        tenant=actor.tenant_id,
        scopes=frozenset(),
        policy_version="policy:one",
        audience="arbora",
    )


def _lease(session: GraphSession) -> CredentialLease:
    lease = session.actor.credential_lease
    assert lease is not None
    return lease


def _issue(monkeypatch: pytest.MonkeyPatch, clock: Clock, roles: Any) -> list[int]:
    issued: list[int] = []

    def renewed(_config: Any) -> ActorContext:
        issued.append(int(clock.now))
        return _actor(clock, roles() if callable(roles) else roles)

    monkeypatch.setattr(bootstrap, "_renewed_process_actor", renewed)
    return issued


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_authority_renews_across_many_token_lifetimes(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    """Twelve hours of 300 s tokens, with the IdP reordering and adding roles."""
    session = _session(clock)
    grants = iter(
        [tuple(reversed(ROLES))] * 20 + [(*ROLES, "source:ingest", "kv:read")] * 1000
    )
    issued = _issue(monkeypatch, clock, lambda: next(grants))
    deadline = clock.now + 12 * 3600
    while clock.now < deadline:
        clock.now = _lease(session).expires_at - 29
        with pytest.raises(SessionExpiredError):
            session.ensure_authority_current(minimum_ttl_seconds=30)
        assert bootstrap._refresh_process_authority(session) is session
        session.ensure_authority_current(minimum_ttl_seconds=30)
    assert len(issued) > 140
    # The running session keeps its original, narrower role set.
    assert session.actor.roles == ROLES


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_revoked_role_fails_closed_with_a_reason(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    session = _session(clock)
    runtime_health.register_process_lease(_lease(session))
    _issue(monkeypatch, clock, ROLES[:-1])
    clock.now = _lease(session).expires_at - 10
    caplog.set_level(logging.ERROR, logger=bootstrap.logger.name)
    stop = threading.Event()
    monkeypatch.setattr(bootstrap, "_PROCESS_AUTHORITY_STOP", stop)
    # Revoked is terminal: the loop ends on its own, without waiting.
    bootstrap._process_authority_refresh_loop(session)
    assert _lease(session).state is LeaseState.REVOKED
    assert runtime_health.readiness_reason() == "process_authority_roles_revoked"
    message = caplog.records[-1].getMessage()
    assert "code=roles_revoked" in message
    assert "changed during renewal" in message
    with pytest.raises(bootstrap.ProcessAuthorityRenewalError) as raised:
        bootstrap._refresh_process_authority(session)
    assert raised.value.code == "roles_revoked"


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_acquisition_failure_logs_its_cause_without_secrets(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, caplog: pytest.LogCaptureFixture
) -> None:
    session = _session(clock)
    token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJl"  # sanitizer:ignore - synthetic unsigned JWT fixture for masking

    def failing(_config: Any) -> ActorContext:
        try:
            refused = (
                f"token endpoint refused Bearer {token}"  # sanitizer:ignore - synthetic
            )
            raise ConnectionError(refused)
        except ConnectionError as exc:
            raise RuntimeError("Graph process identity acquisition failed") from exc

    monkeypatch.setattr(bootstrap, "_renewed_process_actor", failing)
    runtime_health.register_process_lease(_lease(session))
    clock.now = _lease(session).expires_at - 10
    caplog.set_level(logging.ERROR, logger=bootstrap.logger.name)
    stop = threading.Event()
    monkeypatch.setattr(bootstrap, "_PROCESS_AUTHORITY_STOP", stop)
    monkeypatch.setattr(stop, "wait", lambda _seconds: stop.set())
    bootstrap._process_authority_refresh_loop(session)
    message = caplog.records[-1].getMessage()
    assert "code=acquisition_failed" in message
    assert (
        "RuntimeError: Graph process identity acquisition failed <- "
        "ConnectionError: token endpoint refused" in message
    )
    assert token not in message and "c2lnbmF0dXJl" not in message
    assert runtime_health.readiness_reason() == "process_authority_acquisition_failed"

    _issue(monkeypatch, clock, ROLES)
    stop.clear()
    bootstrap._process_authority_refresh_loop(session)
    assert _lease(session).state is LeaseState.LIVE
    assert runtime_health.readiness_reason() is None


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_identity_change_is_refused(clock: Clock) -> None:
    left = _actor(clock)
    assert (
        bootstrap._process_authority_change(left, replace(left, roles=ROLES[::-1]))
        is None
    )
    assert (
        bootstrap._process_authority_change(left, replace(left, tenant_id="other"))
        == "identity_changed"
    )
    assert (
        bootstrap._process_authority_change(left, replace(left, groups=("g",)))
        == "groups_changed"
    )


@pytest.mark.spec("ARBORA-IDENTITY-R027.3")
@pytest.mark.parametrize("offset", [0, 1])
def test_request_path_never_reacquires_an_expired_lease(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, offset: int
) -> None:
    """A request fails closed on an expired lease; only the supervisor recovers it."""
    session = _session(clock)
    issued = _issue(monkeypatch, clock, ROLES)
    clock.now = _lease(session).expires_at + offset
    with pytest.raises(SessionExpiredError):
        bootstrap._refresh_process_authority(session, from_request=True)
    assert issued == []
    assert _lease(session).state is LeaseState.EXPIRED
    # The supervisor path calls the identity provider and recovers.
    assert bootstrap._refresh_process_authority(session) is session
    assert issued == [int(clock.now)]
    session.ensure_authority_current(minimum_ttl_seconds=30)


@pytest.mark.spec("ARBORA-IDENTITY-R027.3")
def test_acquisition_crossing_expiry_recovers_with_the_new_expiry(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    session = _session(clock)
    expiry = _lease(session).expires_at
    clock.now = expiry - 10

    def slow_identity(_config: Any) -> ActorContext:
        clock.now = expiry
        return _actor(clock)

    monkeypatch.setattr(bootstrap, "_renewed_process_actor", slow_identity)
    assert bootstrap._refresh_process_authority(session) is session
    assert _lease(session).state is LeaseState.LIVE
    assert _lease(session).expires_at == expiry + 300


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_revocation_survives_restored_roles_and_new_session(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    session = _session(clock)
    clock.now = _lease(session).expires_at - 10
    _issue(monkeypatch, clock, ROLES[:-1])
    with pytest.raises(bootstrap.ProcessAuthorityRenewalError):
        bootstrap._refresh_process_authority(session)
    issued = _issue(monkeypatch, clock, ROLES)
    with pytest.raises(bootstrap.ProcessAuthorityRenewalError, match="changed"):
        bootstrap._refresh_process_authority(session)
    assert issued == []
    with pytest.raises(SessionExpiredError):
        session.ensure_authority_current()
    # A newly authenticated process gets a new lease, not the revoked object.
    replacement = _session(clock)
    replacement.ensure_authority_current()
    assert _lease(replacement) is not _lease(session)


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_shared_lease_recovers_expiry_and_rejects_revocation(clock: Clock) -> None:
    from arbora.security.actor_context import CredentialExpiredError

    expired = CredentialLease(int(clock.now))
    revoked = CredentialLease(int(clock.now) + 300)
    revoked.revoke("roles_revoked")
    for lease in (expired, revoked):
        with pytest.raises(CredentialExpiredError):
            lease.ensure_current()
    with pytest.raises(CredentialExpiredError):
        revoked.renew(int(clock.now) + 600)
    # ARBORA-IDENTITY-R027.3: an expired lease takes the recovery edge.
    expired.renew(int(clock.now) + 600)
    expired.ensure_current()


def _gateway_issuer(
    monkeypatch: pytest.MonkeyPatch, validated: Callable[[str], ActorContext]
) -> None:
    from arbora.security import request_identity

    monkeypatch.setattr(
        request_identity, "acquire_process_identity_token", lambda _: "fixture"
    )
    monkeypatch.setattr(request_identity, "mint_actor_from_token_sync", validated)


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_gateway_revalidates_before_lease_commit(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    from arbora.gateway.daemon import refresh_process_identity

    session = _session(clock)
    expiry = _lease(session).expires_at
    clock.now = expiry - 10

    def validated(_token: str) -> ActorContext:
        clock.now = expiry
        return _actor(clock)

    _gateway_issuer(monkeypatch, validated)
    with pytest.raises(SessionExpiredError):
        refresh_process_identity(session)
    assert _lease(session).expires_at == expiry


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_concurrent_refresh_reuses_current_identity(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    session = _session(clock)
    clock.now = _lease(session).expires_at - 10
    issued = _issue(monkeypatch, clock, ROLES)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(
            workers.map(bootstrap._refresh_process_authority, [session, session])
        )
    assert all(result is session for result in results)
    assert len(issued) == 1


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_gateway_identity_change_closes_original_lease(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    from arbora.gateway.daemon import refresh_process_identity

    session = _session(clock)
    clock.now = _lease(session).expires_at - 10
    _gateway_issuer(monkeypatch, lambda _: replace(_actor(clock), tenant_id="other"))
    with pytest.raises(RuntimeError, match="changed during renewal"):
        refresh_process_identity(session)
    assert _lease(session).revocation_reason == "identity_changed"
    with pytest.raises(SessionExpiredError):
        session.ensure_authority_current()


class FakeIdentityProvider:
    """Refuses every token request until ``recovers_at``; then mints ``identity``."""

    def __init__(self, clock: Clock, recovers_at: float) -> None:
        self.clock = clock
        self.recovers_at = recovers_at
        self.identity: Callable[[], ActorContext] = lambda: _actor(clock)
        self.calls: list[float] = []

    def __call__(self, _config: Any) -> ActorContext:
        self.calls.append(self.clock.now)
        if self.clock.now < self.recovers_at:
            raise ConnectionError("identity provider unavailable")
        return self.identity()


class SteppedStop(threading.Event):
    """A stop event whose ``wait`` advances the fake clock instead of sleeping."""

    def __init__(self, clock: Clock, until: Callable[[], bool]) -> None:
        super().__init__()
        self.clock = clock
        self.until = until
        self.waits: list[float] = []

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        self.waits.append(float(timeout or 0.0))
        self.clock.now += float(timeout or 0.0)
        if self.until() or len(self.waits) > 10_000:
            self.set()
        return self.is_set()


@pytest.mark.spec("ARBORA-IDENTITY-R027.2")
def test_renewal_starts_at_half_lifetime_and_backs_off_with_jitter(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    import random

    seeded = random.Random(7)
    delays = [bootstrap.next_delay(attempt, seeded) for attempt in range(1, 9)]
    for delay, base in zip(delays, (1, 2, 4, 8, 16, 30, 30, 30), strict=True):
        assert 0.5 * base <= delay <= 1.0 * base

    session = _session(clock)
    start = clock.now
    provider = FakeIdentityProvider(clock, recovers_at=float("inf"))
    monkeypatch.setattr(bootstrap, "_renewed_process_actor", provider)
    stop = SteppedStop(clock, until=lambda: len(provider.calls) >= 8)
    monkeypatch.setattr(bootstrap, "_PROCESS_AUTHORITY_STOP", stop)
    bootstrap._process_authority_refresh_loop(session, rng=random.Random(7))

    # The first renewal attempt is at half the 300 s lifetime, not at 30 s left.
    assert provider.calls[0] == start + 150
    # After each failure the loop waits next_delay(attempt) on the same seed.
    assert stop.waits[-8:] == pytest.approx(delays)
    # Before the half-lifetime point a Live lease only idles (60 s at most).
    assert sum(stop.waits[:-8]) == pytest.approx(150)
    lease = _lease(session)
    assert lease.state is LeaseState.DEGRADED  # still before the 300 s expiry
    assert lease.attempts == 8

    # The attempt count resets on idp_ok.
    provider.recovers_at = clock.now
    stop.clear()
    stop.until = lambda: _lease(session).state is LeaseState.LIVE
    bootstrap._process_authority_refresh_loop(session, rng=random.Random(7))
    assert (lease.state, lease.attempts, lease.failure_code) == (
        LeaseState.LIVE,
        0,
        None,
    )


@pytest.mark.spec("ARBORA-IDENTITY-R027.3")
def test_outage_longer_than_ttl_recovers_without_restart(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    """Regression for the 2026-11-06 outage: the IdP is down for 3 token lifetimes."""
    session = _session(clock)
    lease = _lease(session)
    start = clock.now
    provider = FakeIdentityProvider(clock, recovers_at=start + 3 * 300)
    monkeypatch.setattr(bootstrap, "_renewed_process_actor", provider)
    seen: list[LeaseState] = []

    def recovered() -> bool:
        seen.append(lease.state)
        return clock.now > provider.recovers_at and lease.state is LeaseState.LIVE

    stop = SteppedStop(clock, until=recovered)
    monkeypatch.setattr(bootstrap, "_PROCESS_AUTHORITY_STOP", stop)
    bootstrap._process_authority_refresh_loop(session)

    assert LeaseState.DEGRADED in seen and LeaseState.EXPIRED in seen
    assert seen.index(LeaseState.DEGRADED) < seen.index(LeaseState.EXPIRED)
    assert lease.state is LeaseState.LIVE
    # The same captured session and lease object, with the newly minted expiry.
    assert session.actor.credential_lease is lease
    assert lease.expires_at > clock.now + 30
    session.ensure_authority_current(minimum_ttl_seconds=30)
    assert session.actor.roles == ROLES
    # The provider was retried throughout the outage, never abandoned.
    assert max(b - a for a, b in itertools.pairwise(provider.calls)) <= 30

    # A changed identity at recovery ends in Revoked instead.
    other = _session(clock)
    other_lease = _lease(other)
    provider.recovers_at = clock.now + 3 * 300
    provider.identity = lambda: replace(_actor(clock), tenant_id="other")
    stop.clear()
    stop.until = lambda: other_lease.state is LeaseState.REVOKED
    bootstrap._process_authority_refresh_loop(other)
    assert other_lease.state is LeaseState.REVOKED
    assert other_lease.revocation_reason == "identity_changed"
    with pytest.raises(SessionExpiredError):
        other.ensure_authority_current()


class Lockstep(threading.Event):
    """A stop event that hands control to the test at every supervisor wait."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self.clock = clock
        self.asked: queue.Queue[float] = queue.Queue()
        self.resume: queue.Queue[None] = queue.Queue()

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        self.asked.put(float(timeout or 0.0))
        self.resume.get(timeout=30)
        self.clock.now += float(timeout or 0.0)
        return self.is_set()


@pytest.mark.spec("ARBORA-IDENTITY-R027.7")
def test_supervisor_drives_the_lease_through_an_outage_and_ready_follows(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    """The real supervisor thread, loop and ``/health/ready`` route, with a fake IdP."""
    from starlette.testclient import TestClient

    from arbora.mcp_server import runtime

    session = _session(clock)
    lease = _lease(session)
    provider = FakeIdentityProvider(clock, recovers_at=clock.now + 3 * 300)
    monkeypatch.setattr(bootstrap, "_renewed_process_actor", provider)

    async def health() -> dict[str, Any]:
        # The engine probe runs under the process authority, so it fails
        # once the lease is no longer usable.
        try:
            session.ensure_authority_current()
            status = "healthy"
        except SessionExpiredError:
            status = "unhealthy"
        return {"checks": [{"name": "engine", "status": status}]}

    monkeypatch.setattr(runtime_health, "collect_health_async", health)
    _args, mcp, _middlewares = runtime._build_server(bootstrap=False)
    client = TestClient(mcp.http_app())

    stop = Lockstep(clock)
    monkeypatch.setattr(bootstrap, "_PROCESS_AUTHORITY_STOP", stop)
    bootstrap._start_process_authority_supervisor(session)
    assert runtime_health.process_lease() is lease
    observed: dict[LeaseState, dict[str, Any]] = {}
    try:
        for _ in range(10_000):
            stop.asked.get(timeout=30)  # the supervisor is parked in wait()
            state = lease.state
            if state not in observed or (
                state is LeaseState.LIVE and clock.now > provider.recovers_at
            ):
                body = client.get("/health/ready").json()
                # Only the keys this row owns; catalog state is process-shared.
                observed[state] = {
                    key: body[key]
                    for key in ("status", "failing", "reason")
                    if key in body
                }
            if state is LeaseState.LIVE and clock.now > provider.recovers_at:
                stop.set()
                stop.resume.put(None)
                break
            stop.resume.put(None)
    finally:
        stop.set()
        stop.resume.put(None)
        bootstrap._stop_process_authority_supervisor()

    assert observed[LeaseState.DEGRADED] == {
        "status": "ready",
        "reason": "process_authority_acquisition_failed",
    }
    assert observed[LeaseState.EXPIRED] == {
        "status": "not_ready",
        "failing": ["engine"],
        "reason": "process_authority_acquisition_failed",
    }
    assert observed[LeaseState.LIVE] == {"status": "ready"}
    assert lease.state is LeaseState.LIVE
    assert runtime_health.process_lease() is None
