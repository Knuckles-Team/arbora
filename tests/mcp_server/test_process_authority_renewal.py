"""The process authority renews indefinitely and reports why when it cannot."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from arbora.api.contracts.session import GraphSession, SessionExpiredError
from arbora.mcp_server import bootstrap
from arbora.observability import runtime_health
from arbora.security.actor_context import ActorContext, ActorType, CredentialLease

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
    runtime_health.record_process_authority_failure(None)


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
    _issue(monkeypatch, clock, ROLES[:-1])
    clock.now = _lease(session).expires_at - 10
    with pytest.raises(bootstrap.ProcessAuthorityRenewalError) as raised:
        bootstrap._refresh_process_authority(session)
    assert raised.value.code == "roles_revoked"

    caplog.set_level(logging.ERROR, logger=bootstrap.logger.name)
    stop = threading.Event()
    monkeypatch.setattr(bootstrap, "_PROCESS_AUTHORITY_STOP", stop)
    monkeypatch.setattr(stop, "wait", lambda _seconds: stop.set())
    bootstrap._process_authority_refresh_loop(session)
    assert runtime_health.readiness_reason() == "process_authority_roles_revoked"
    message = caplog.records[-1].getMessage()
    assert "code=roles_revoked" in message
    assert "changed during renewal" in message


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


@pytest.mark.spec("ARBORA-IDENTITY-R027")
@pytest.mark.parametrize("offset", [0, 1])
def test_expired_authority_never_reacquires(
    monkeypatch: pytest.MonkeyPatch, clock: Clock, offset: int
) -> None:
    session = _session(clock)
    issued = _issue(monkeypatch, clock, ROLES)
    clock.now = _lease(session).expires_at + offset
    with pytest.raises(SessionExpiredError):
        bootstrap._refresh_process_authority(session)
    assert issued == []


@pytest.mark.spec("ARBORA-IDENTITY-R027")
def test_acquisition_crossing_expiry_cannot_extend_lease(
    monkeypatch: pytest.MonkeyPatch, clock: Clock
) -> None:
    session = _session(clock)
    expiry = _lease(session).expires_at
    clock.now = expiry - 10

    def slow_identity(_config: Any) -> ActorContext:
        clock.now = expiry
        return _actor(clock)

    monkeypatch.setattr(bootstrap, "_renewed_process_actor", slow_identity)
    with pytest.raises(SessionExpiredError):
        bootstrap._refresh_process_authority(session)
    assert _lease(session).expires_at == expiry


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
def test_shared_lease_rejects_expiry_and_revocation(clock: Clock) -> None:
    from arbora.security.actor_context import CredentialExpiredError

    expired = CredentialLease(int(clock.now))
    revoked = CredentialLease(int(clock.now) + 300)
    revoked.revoke("roles_revoked")
    for lease in (expired, revoked):
        with pytest.raises(CredentialExpiredError):
            lease.renew(int(clock.now) + 600)
        with pytest.raises(CredentialExpiredError):
            lease.ensure_current()


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
