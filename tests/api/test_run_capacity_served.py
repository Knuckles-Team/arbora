"""Run admission reserves engine capacity, over the served app.

Row ARBORA-HOST-R031.1: ``agents.plan_run`` reaches ``_admit_native_plan_run`` through
the real HTTP app and invoke pipeline. The capacity cell and lease are the engine's:
the engine methods ``AcquireCapacity`` and ``ReleaseCapacity`` are recorded doubles
that answer the generated contract shapes. Arbora keeps no counter of its own.
"""

# ruff: noqa: F811

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.api._loopback_engine import Loopback, loopback  # noqa: E402,F401
from tests.api.test_plan_run_ops import BINDING_PLAN  # noqa: E402
from tests.api.test_run_admission_served import (  # noqa: E402,F401
    ITEM,
    OP,
    POLICY,
    _reset,
    loopback_bindings,
)
from tests.test_served_operation_api import served  # noqa: E402,F401

pytestmark = pytest.mark.real_run_capacity


def lease(request: dict[str, Any]) -> dict[str, Any]:
    demand = request["demands"][0]
    return {
        "actor_digest": "a" * 64,
        "amount": demand["amount"],
        "cell_id": demand["cell_id"],
        "expires_at_ms": request["now_ms"] + request["ttl_ms"],
        "fence_token": 7,
        "idempotency_key": request["idempotency_key"],
        "issued_at_ms": request["now_ms"],
        "lease_epoch": 3,
        "lease_id": "lease-one",
        "priority": request["priority"],
        "renewed_count": 0,
        "resource_class": demand["resource_class"],
        "schema_version": 1,
        "state": "active",
        "tenant_ref": request["tenant_ref"],
        "work_item_id": request["work_item_id"],
    }


class Cell:
    """The engine's answers; the double holds the lease the way the engine does."""

    def __init__(self, *, decision: str = "accepted") -> None:
        self.decision = decision
        self.held: list[dict[str, Any]] = []
        self.released: list[dict[str, Any]] = []

    def acquire(self, params: dict[str, Any], _principal: str) -> dict[str, Any]:
        from medulla.generated.models import CapacityAcquireRequest

        request = CapacityAcquireRequest.model_validate(params["request"])
        granted = self.decision in {"accepted", "replayed"}
        if granted:
            self.held.append(lease(params["request"]))
        return {
            "available": [],
            "decision": self.decision,
            "leases": [self.held[-1]] if granted else [],
            "schema_version": request.schema_version.value,
        }

    def release(self, params: dict[str, Any], _principal: str) -> dict[str, Any]:
        from medulla.generated.models import CapacityLeaseMutationRequest

        CapacityLeaseMutationRequest.model_validate(params["request"])
        self.released.extend(params["request"]["leases"])
        return {"decision": "released", "leases": [], "schema_version": "1"}


def record_native_layer(monkeypatch: pytest.MonkeyPatch, seen: list[str]) -> None:
    import arbora.api.ops.plans as plans
    import arbora.work_items as work_items

    def get(_engine: Any, requested: str) -> Any:
        seen.append("get")
        return {
            "kind": "orchestrator_task",
            "status": "ready",
            "metadata": POLICY.subjects[-1]["metadata"],
        }

    def claim(_engine: Any, requested: str, *, token: str, lease_ttl_s: float) -> Any:
        seen.append("claim")
        return {
            "claimed": True,
            "tenant": POLICY.subjects[-1]["tenant"],
            "lease_holder_ref": token,
            "work_item_id": requested,
            "lease_epoch": 2,
            "fencing_token": 9,
            "lease_expires_at_ms": int(time.time() * 1000) + 600_000,
        }

    def commit(*args: Any, **kwargs: Any) -> str:
        seen.append("commit")
        return "committed"

    async def execute(context: Any, native: Any, plan: Any, proposal: Any, parsed: Any):
        seen.append("run")
        return {"plan_digest": plan["plan_digest"]}, {"status": "completed"}

    monkeypatch.setattr(work_items, "get_work_item", get)
    monkeypatch.setattr(work_items, "claim_specific", claim)
    monkeypatch.setattr(work_items, "commit_result", commit)
    monkeypatch.setattr(plans, "_publish_and_execute_native_plan", execute)


def serve(loopback: Loopback, cell: Cell) -> None:
    from arbora.api.ops import run_capacity

    loopback.serve_unreleased(
        run_capacity,
        {"AcquireCapacity": cell.acquire, "ReleaseCapacity": cell.release},
    )


@pytest.mark.spec("ARBORA-HOST-R031.1")
def test_agents_plan_run_reserves_and_releases_engine_capacity(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    cell, seen = Cell(), []
    serve(loopback, cell)
    record_native_layer(monkeypatch, seen)
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="reserve-1")
    assert reply.status_code == 200, reply.text
    assert Loopback.value(reply)["run"]["status"] == "completed"
    methods = [call[0] for call in loopback.engine_calls]
    assert methods == ["AcquireCapacity", "ReleaseCapacity"]
    # The lease is taken for the run's own identity, before the claim.
    acquire = loopback.engine_calls[0][1]["request"]
    assert acquire["idempotency_key"] == POLICY.subjects[0]["run_id"]
    assert acquire["demands"] == [
        {"amount": 1, "cell_id": "run:agents", "resource_class": "worker"}
    ]
    assert seen.index("claim") < seen.index("commit")
    # The release names the exact fence the engine issued.
    assert cell.released == [
        {"fence_token": 7, "lease_epoch": 3, "lease_id": "lease-one"}
    ]


@pytest.mark.spec("ARBORA-HOST-R031.1")
@pytest.mark.parametrize("decision", ["exhausted", "backpressure"])
def test_exhausted_cell_throttles_the_run_before_any_claim(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    cell, seen = Cell(decision=decision), []
    serve(loopback, cell)
    record_native_layer(monkeypatch, seen)
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key=f"throttle-{decision}")
    assert reply.status_code == 429, reply.text
    assert reply.json()["error"]["code"] == "CAPACITY_THROTTLED"
    assert "claim" not in seen and "run" not in seen
    assert [c[0] for c in loopback.engine_calls] == ["AcquireCapacity"]
    assert cell.held == [] and cell.released == []


@pytest.mark.spec("ARBORA-HOST-R031.1")
def test_unserved_capacity_method_refuses_unavailable_not_a_local_count(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    loopback.serve({})
    seen: list[str] = []
    record_native_layer(monkeypatch, seen)
    monkeypatch.setattr("medulla.generated.METHOD_IDS", frozenset())
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="no-capacity")
    assert reply.status_code == 503, reply.text
    assert "claim" not in seen


@pytest.mark.spec("ARBORA-HOST-R031.1")
def test_failed_claim_gives_the_lease_back(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    import arbora.work_items as work_items

    cell, seen = Cell(), []
    serve(loopback, cell)
    record_native_layer(monkeypatch, seen)
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: None)
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="claim-denied")
    assert reply.status_code == 503, reply.text
    assert [c[0] for c in loopback.engine_calls] == [
        "AcquireCapacity",
        "ReleaseCapacity",
    ]
    assert len(cell.released) == 1


@pytest.mark.spec("ARBORA-HOST-R031.1")
def test_a_failed_release_never_turns_a_committed_run_into_an_error(
    loopback: Loopback,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    cell, seen = Cell(), []
    serve(loopback, cell)
    record_native_layer(monkeypatch, seen)

    def broken(params: dict[str, Any], _principal: str) -> Any:
        raise RuntimeError("engine unreachable")

    from arbora.api.ops import run_capacity

    loopback.serve_unreleased(
        run_capacity, {"AcquireCapacity": cell.acquire, "ReleaseCapacity": broken}
    )
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="release-broken")
    assert reply.status_code == 200, reply.text
    assert "commit" in seen
    assert "expires at its TTL" in caplog.text
