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
