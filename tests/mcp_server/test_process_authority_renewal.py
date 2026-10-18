"""The process authority renews indefinitely and reports why when it cannot."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterator
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
