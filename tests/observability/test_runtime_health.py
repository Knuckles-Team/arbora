"""The runtime health core reports bound probes and fails closed without them."""

from __future__ import annotations

import pytest

from arbora.observability import runtime_health as rh


@pytest.fixture(autouse=True)
def _clean_probes():
    rh.reset_health_probes()
    rh._FLEET_SUPERVISION_CACHE.invalidate()
    yield
    rh.reset_health_probes()
    rh._FLEET_SUPERVISION_CACHE.invalidate()


def _check(report, name):
    return next(check for check in report["checks"] if check["name"] == name)


def test_unbound_mandatory_probes_make_the_process_not_ready():
    report = rh.collect_health()
    assert _check(report, "engine")["status"] == "unhealthy"
    assert _check(report, "kg_mirrors")["status"] == "not_configured"
    assert not rh.is_overall_healthy(report)


def test_bound_probes_receive_the_readiness_session_and_decide_readiness():
    seen = []

    def probe(session):
        seen.append(session)
        return {"status": "ok"}

    rh.register_health_probe("engine", probe)
    rh.register_health_probe("fleet_supervision", probe)
    owner = rh.claim_readiness_authority("session-1")
    try:
        report = rh.collect_health()
    finally:
        assert rh.release_readiness_authority(owner)
    assert rh.is_overall_healthy(report)
    assert set(seen) == {"session-1"}


@pytest.mark.parametrize("failure", [RuntimeError("down"), None])
def test_a_failing_probe_is_unhealthy_with_its_reason(failure):
    def probe(_session):
        if failure is not None:
            raise failure
        return {"status": "unhealthy", "reason": "engine unreachable"}

    rh.register_health_probe("engine", probe)
    check = _check(rh.collect_health(), "engine")
    assert check["status"] == "unhealthy"
    assert check["reason"]


def test_readiness_authority_has_one_owner():
    owner = rh.claim_readiness_authority("s")
    try:
        with pytest.raises(RuntimeError):
            rh.claim_readiness_authority("other")
        assert not rh.release_readiness_authority(object())
    finally:
        assert rh.release_readiness_authority(owner)


@pytest.mark.spec("ARBORA-IDENTITY-R027.5")
def test_readiness_reason_follows_lease_state_with_no_separate_global():
    from arbora.observability import runtime_health
    from arbora.security.actor_context import CredentialLease, LeaseEvent, LeaseState

    assert not hasattr(runtime_health, "_PROCESS_AUTHORITY_FAILURE")
    assert not hasattr(runtime_health, "record_process_authority_failure")

    issued = 1_800_000_000.0
    lease = CredentialLease(int(issued) + 300, issued_at=issued)
    runtime_health.register_process_lease(lease)
    try:
        seen = {lease.state: runtime_health.readiness_reason()}
        steps = [
            (LeaseEvent.TICK, issued + 150, {}),
            (LeaseEvent.RENEW_START, issued + 150, {}),
            (LeaseEvent.IDP_ERR, issued + 151, {"code": "acquisition_failed"}),
            (LeaseEvent.TICK, issued + 300, {}),
        ]
        for event, now, extra in steps:
            lease.apply(event, now, **extra)
            seen[lease.state] = runtime_health.readiness_reason()
        assert seen == {
            LeaseState.LIVE: None,
            LeaseState.RENEW_DUE: None,
            LeaseState.RENEWING: None,
            LeaseState.DEGRADED: "process_authority_acquisition_failed",
            LeaseState.EXPIRED: "process_authority_acquisition_failed",
        }
        lease.apply(LeaseEvent.IDENTITY_CHANGED, issued + 301, code="roles_revoked")
        assert runtime_health.readiness_reason() == "process_authority_roles_revoked"

        # A code outside the closed vocabulary never reaches the route.
        other = CredentialLease(int(issued) + 300, issued_at=issued)
        other.revoke("free text from somewhere")
        runtime_health.register_process_lease(other)
        assert runtime_health.readiness_reason() == "process_authority_renewal_failed"
    finally:
        runtime_health.register_process_lease(None)
    assert runtime_health.readiness_reason() is None
