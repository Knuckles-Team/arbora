"""Drift repair and embedding operations (ARBORA-OPS-R020.4, R020.5)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ingest
from arbora.api.registry import Effect
from tests.api._ops_support import service_context


class _Runner:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.previewed = False

    async def list_drift(self, *, tenant, source_id=None):
        return {"drift": [], "tenant": tenant}

    async def get_drift(self, *, tenant, drift_id):
        return {"drift_id": drift_id}

    async def propose_repair(self, *, tenant, drift_id, idempotency_key):
        self.events.append(("propose", drift_id))
        self.previewed = True
        return {"repair_id": "r1", "preview": {"changes": 2}}

    async def get_repair(self, *, tenant, repair_id):
        if repair_id != "r1":
            return None
        return {
            "repair_id": "r1",
            "preview": {"changes": 2} if self.previewed else None,
        }

    async def apply_repair(self, *, tenant, repair_id, approval_ref, idempotency_key):
        self.events.append(("apply", approval_ref))
        return {"repair_id": repair_id, "applied": True, "approval_ref": approval_ref}

    async def admit_embedding(self, *, tenant, model, dimensions, idempotency_key):
        self.events.append(("admit", model))
        return {"job_id": "j1", "status": "queued"}

    async def reembed(self, *, tenant, source_id, model, idempotency_key):
        self.events.append(("reembed", source_id))
        return {"job_id": "j2", "status": "queued"}


def _op(op_id: str):
    return next(o for o in ingest.operations() if o.id == op_id)


async def _run(op_id: str, runner: _Runner | None, **params: Any):
    services = {} if runner is None else {"ingest_runner": runner}
    ctx = service_context(tenant="t", principal="p", **services)
    handler = getattr(ingest, _op(op_id).binding.handler.rpartition(".")[2])
    return await handler(ctx, params, _op(op_id))


def test_operations_declare_scopes_and_effects() -> None:
    for op_id in ("ingest.drift.list", "ingest.drift.get"):
        assert _op(op_id).effect is Effect.READ
    for op_id in (
        "ingest.drift.repair_propose",
        "ingest.drift.repair_approve",
        "ingest.embedding.admit",
        "ingest.embedding.reembed",
    ):
        assert _op(op_id).effect is Effect.WRITE
        assert _op(op_id).scopes == {"ingest:write"}


@pytest.mark.asyncio
async def test_repair_previews_then_applies_only_against_approval() -> None:
    assert _op("ingest.drift.repair_approve").audit.value == "event"
    runner = _Runner()
    # Approving a proposal that was never previewed is refused and applies nothing.
    runner.previewed = False
    with pytest.raises(OperationRefused):
        await _run(
            "ingest.drift.repair_approve", runner, repair_id="r1", approval_ref="ap-1"
        )
    assert runner.events == []
    with pytest.raises(LookupError):
        await _run(
            "ingest.drift.repair_approve", runner, repair_id="nope", approval_ref="ap-1"
        )
    proposed = await _run("ingest.drift.repair_propose", runner, drift_id="d1")
    assert proposed["value"]["preview"] == {"changes": 2}
    assert runner.events == [("propose", "d1")]  # propose applied nothing
    applied = await _run(
        "ingest.drift.repair_approve", runner, repair_id="r1", approval_ref="ap-1"
    )
    assert applied["value"]["applied"] is True
    assert runner.events[-1] == ("apply", "ap-1")
    # approval_ref is mandatory, and an absent runner fails closed.
    with pytest.raises(ValueError):
        ingest.IngestRepairApproveParams.model_validate({"repair_id": "r1"})
    with pytest.raises(OperationRefused):
        await _run("ingest.drift.list", None)


@pytest.mark.asyncio
async def test_embedding_operations_reach_runner_and_report_job_state() -> None:
    runner = _Runner()
    admitted = await _run("ingest.embedding.admit", runner, model="m", dimensions=384)
    assert admitted["value"] == {"job_id": "j1", "status": "queued"}
    re = await _run("ingest.embedding.reembed", runner, source_id="s", model="m")
    assert re["value"]["status"] == "queued"
    assert [e[0] for e in runner.events] == ["admit", "reembed"]
    with pytest.raises(OperationRefused):
        await _run("ingest.embedding.reembed", None, source_id="s", model="m")
