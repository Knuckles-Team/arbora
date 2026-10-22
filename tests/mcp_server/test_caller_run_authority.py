"""A run renews its caller's 300 s authority until it ends (ARBORA-IDENTITY-R029)."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from collections.abc import Iterator
from typing import Any

import pytest

from arbora.api.contracts.session import (
    GraphSession,
    SessionExpiredError,
    current_session,
    use_session,
)
from arbora.mcp_server import bootstrap
from arbora.security import run_authority
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.run_authority import (
    CALLER_REVOCATIONS,
    RunCredentialLease,
    RunRenewalPolicy,
    keep_run_authority_current,
    open_run_lease,
)

TTL = 300
SECRET_BEARER = ".".join(["ey" + "J-header", "ey" + "J-claims", "signature"])


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.001)
        await asyncio.sleep(0)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    fake = Clock()
    monkeypatch.setattr(time, "time", fake.time)
    CALLER_REVOCATIONS.clear()
    yield fake
    CALLER_REVOCATIONS.clear()


def _caller(clock: Clock) -> ActorContext:
    """A verified caller bearer: bounded 300 s expiry and no renewable lease."""
    return ActorContext(
        actor_id="user:ada",
        actor_type=ActorType.HUMAN,
        roles=("kg:read",),
        tenant_id="homelab",
        authenticated=True,
        credential_expires_at=int(clock.now) + TTL,
    )


def _session(clock: Clock) -> GraphSession:
    actor = _caller(clock)
    return GraphSession(
        actor=actor,
        tenant=actor.tenant_id,
        scopes=frozenset({"kg:read"}),
        policy_version="policy:one",
        audience="arbora",
    )


async def _run_for(clock: Clock, seconds: float, step: float = 10.0) -> None:
    """A run body that checks authority at every step, like engine calls do."""
    end = clock.now + seconds
    while clock.now < end:
        session = current_session()
        assert session is not None
        session.ensure_authority_current()
        await clock.sleep(step)


def _run_lease() -> RunCredentialLease:
    session = current_session()
    assert session is not None
    lease = session.actor.credential_lease
    assert isinstance(lease, RunCredentialLease)
    return lease


@pytest.fixture
def scope(monkeypatch: pytest.MonkeyPatch, clock: Clock) -> Any:
    """``authority_keepalive_scope`` with the renewer driven by the fake clock."""
    monkeypatch.setattr(
        bootstrap,
        "_AUTHORITY_KEEPALIVE_ACTIVE",
        contextvars.ContextVar("test_keepalive_active", default=False),
    )
    real = keep_run_authority_current

    async def fake_clock_renewer(actor: Any, lease: Any, *, policy: Any) -> int:
        async def yield_only(_seconds: float) -> None:
            # The run body advances the fake clock; the renewer only yields.
            await asyncio.sleep(0)

        return await real(
            actor, lease, policy=policy, clock=clock.time, sleep=yield_only
        )

    monkeypatch.setattr(run_authority, "keep_run_authority_current", fake_clock_renewer)
    return bootstrap.authority_keepalive_scope


@pytest.mark.spec("ARBORA-IDENTITY-R029.1")
def test_run_longer_than_three_ttls_stays_authorized(clock: Clock, scope: Any) -> None:
    session = _session(clock)
    start = clock.now

    async def main() -> RunCredentialLease:
        with use_session(session):
            async with scope():
                lease = _run_lease()
                await _run_for(clock, 4 * TTL)
                return lease

    lease = asyncio.run(main())
    assert clock.now - start >= 4 * TTL
    assert isinstance(lease, RunCredentialLease)
    assert lease.renewals >= 3
    # The run ended, so its lease is closed at once.
    assert lease.revocation_reason == "run_ended"


@pytest.mark.spec("ARBORA-IDENTITY-R029.1")
def test_without_renewal_the_same_run_fails_after_one_ttl(clock: Clock) -> None:
    """Control: the bearer alone dies at 300 s, which is the defect R029 fixes."""
    session = _session(clock)

    async def main() -> None:
        with use_session(session):
            await _run_for(clock, 4 * TTL)

    with pytest.raises(SessionExpiredError):
        asyncio.run(main())


@pytest.mark.spec("ARBORA-IDENTITY-R029.3")
def test_fan_out_children_inherit_the_parent_run_renewal(
    clock: Clock, scope: Any
) -> None:
    session = _session(clock)
    seen: list[Any] = []

    async def child() -> None:
        async with scope():  # nested: a no-op that keeps the parent's lease
            seen.append(_run_lease())
            await _run_for(clock, 3 * TTL + 60)

    async def main() -> Any:
        with use_session(session):
            async with scope():
                parent = _run_lease()
                await asyncio.gather(*(asyncio.create_task(child()) for _ in range(3)))
                return parent

    parent = asyncio.run(main())
    assert seen == [parent, parent, parent]
    assert parent.renewals >= 3


@pytest.mark.spec("ARBORA-IDENTITY-R029.2")
def test_revoking_the_caller_stops_renewal_at_once(clock: Clock, scope: Any) -> None:
    session = _session(clock)

    async def main() -> RunCredentialLease:
        with use_session(session):
            async with scope():
                lease = _run_lease()
                await _run_for(clock, 2 * TTL)
                renewals = lease.renewals
                assert (
                    CALLER_REVOCATIONS.revoke_caller("homelab", "user:ada", "revoked")
                    == 1
                )
                with pytest.raises(SessionExpiredError):
                    current_session().ensure_authority_current()  # type: ignore[union-attr]
                await clock.sleep(2 * TTL)
                assert lease.renewals == renewals
                return lease

    lease = asyncio.run(main())
    assert lease.revocation_reason == "revoked"


@pytest.mark.spec("ARBORA-IDENTITY-R029.2")
def test_a_revocation_check_stops_renewal(clock: Clock) -> None:
    actor = _caller(clock)
    policy = RunRenewalPolicy()
    lease = open_run_lease(actor, policy=policy, clock=clock.time)
    calls = 0

    async def check(_actor: ActorContext) -> str | None:
        nonlocal calls
        calls += 1
        return "session_revoked" if clock.now > 1_800_000_000 + 2 * TTL else None

    renewals = asyncio.run(
        keep_run_authority_current(
            actor,
            lease,
            policy=policy,
            revocation_check=check,
            audit=lambda _r: None,
            clock=clock.time,
            sleep=clock.sleep,
        )
    )
    assert lease.revocation_reason == "session_revoked"
    assert renewals >= 1
    with pytest.raises(PermissionError):
        lease.ensure_current()


@pytest.mark.spec("ARBORA-IDENTITY-R029.2")
def test_cancelling_the_run_stops_renewal(clock: Clock, scope: Any) -> None:
    session = _session(clock)
    leases: list[RunCredentialLease] = []

    async def run() -> None:
        with use_session(session):
            async with scope():
                leases.append(_run_lease())
                await _run_for(clock, 100 * TTL)

    async def main() -> None:
        task = asyncio.create_task(run())
        while clock.now < 1_800_000_000 + 2 * TTL:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())
    (lease,) = leases
    assert lease.revocation_reason == "run_cancelled"
    renewals = lease.renewals
    clock.now += 10 * TTL
    assert lease.renewals == renewals
    with pytest.raises(PermissionError):
        lease.ensure_current()


@pytest.mark.spec("ARBORA-IDENTITY-R029.2")
def test_renewal_stops_at_the_maximum_run_lifetime(clock: Clock) -> None:
    actor = _caller(clock)
    policy = RunRenewalPolicy(max_run_seconds=3 * TTL)
    lease = open_run_lease(actor, policy=policy, clock=clock.time)
    events: list[str] = []
    asyncio.run(
        keep_run_authority_current(
            actor,
            lease,
            policy=policy,
            audit=lambda r: events.append(r["event"]),
            clock=clock.time,
            sleep=clock.sleep,
        )
    )
    assert lease.expires_at == lease.run_deadline
    assert events[-1] == "lifetime_reached"
    clock.now = lease.run_deadline
    with pytest.raises(PermissionError):
        lease.ensure_current()


@pytest.mark.spec("ARBORA-IDENTITY-R029.3")
def test_renewal_is_audited_and_never_logs_a_token(
    clock: Clock, scope: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    session = _session(clock)

    async def main() -> None:
        with use_session(session):
            async with scope():
                logging.getLogger("test").debug("caller presented a bearer")
                await _run_for(clock, 2 * TTL)

    asyncio.run(main())
    audit = [
        r.getMessage() for r in caplog.records if r.name == "arbora.audit.run_authority"
    ]
    assert any("event=started" in m for m in audit)
    assert any("event=renewed" in m and "principal=user:ada" in m for m in audit)
    assert any("event=run_ended" in m for m in audit)
    assert all(SECRET_BEARER not in r.getMessage() for r in caplog.records)
    assert all("eyJ" not in m and "Bearer" not in m for m in audit)


@pytest.mark.spec("ARBORA-IDENTITY-R029.1")
def test_process_renewal_never_touches_a_run_lease(clock: Clock, scope: Any) -> None:
    """The R027 process path must not reacquire the process identity for a caller."""
    session = _session(clock)

    def must_not_run(_config: Any) -> Any:
        raise AssertionError("process identity reacquired for a caller run")

    async def main() -> None:
        with use_session(session):
            async with scope():
                run_session = current_session()
                assert run_session is not None
                with pytest.raises(bootstrap.ProcessAuthorityRenewalError) as err:
                    bootstrap._refresh_process_authority(run_session)
                assert err.value.code == "not_renewable"
                assert (
                    await bootstrap._ensure_process_authority_current() is run_session
                )
                lease = run_session.actor.credential_lease
                assert lease is not None
                assert lease.revocation_reason is None

    original = bootstrap._renewed_process_actor
    bootstrap._renewed_process_actor = must_not_run  # type: ignore[assignment]
    try:
        asyncio.run(main())
    finally:
        bootstrap._renewed_process_actor = original
