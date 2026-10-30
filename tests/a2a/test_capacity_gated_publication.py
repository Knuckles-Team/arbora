"""ARBORA-FLEET-R009.2: routing/composition publish only through acquired capacity."""

from __future__ import annotations

import pytest

from arbora.a2a.composition import CapacityGatedPublisher
from arbora.control_plane.runs.admission import (
    CapacityAcquisition,
    CapacityDeniedError,
)


@pytest.mark.spec("ARBORA-FLEET-R009.2")
def test_evaluate_commit_grant_publish_redecision_and_stop_release() -> None:
    capacity = CapacityAcquisition(capacity={"slots": 1})
    publisher = CapacityGatedPublisher(capacity)
    log: list[str] = []

    def run(run_id: str):
        return publisher.publish(
            run_id,
            {"slots": 1},
            evaluate=lambda: log.append(f"eval:{run_id}") or run_id,
            commit=lambda c: log.append(f"commit:{c}") or {"run": c, "evidence": "syn"},
            publish=lambda c: log.append(f"pub:{c['run']}") or c["run"],
        )

    first = run("r1")
    assert first.published and first.published_value == "r1"
    assert capacity.held("r1") == {"slots": 1}
    assert log == ["eval:r1", "commit:r1", "pub:r1"]

    denied = run("r2")
    assert not denied.published and denied.redecision_required
    assert "pub:r2" not in log

    publisher.stop("r1")
    assert capacity.held("r1") == {}
    retried = run("r2")
    assert retried.published and capacity.held("r2") == {"slots": 1}

    # Second denial for the same run fails closed.
    publisher.stop("r2")
    assert run("r3").published
    assert run("r4").redecision_required
    with pytest.raises(CapacityDeniedError):
        run("r4")


@pytest.mark.spec("ARBORA-FLEET-R009.2")
def test_failed_publish_releases_held_capacity() -> None:
    capacity = CapacityAcquisition(capacity={"slots": 1})
    publisher = CapacityGatedPublisher(capacity)

    def boom(_: object) -> None:
        raise RuntimeError("publish failed")

    with pytest.raises(RuntimeError):
        publisher.publish(
            "r1", {"slots": 1}, evaluate=lambda: 1, commit=lambda c: c, publish=boom
        )
    assert capacity.held("r1") == {}
