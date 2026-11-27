"""Ingest sync, drift repair and re-embedding served over HTTP and MCP (ARBORA-OPS-R020).

What is real: the served app and MCP surface, authentication, the invoke pipeline with
its replay owner and audit, and the ingest operations. What is recorded: the composed
ingestion runner, which keeps one durable record per idempotency key as the engine does.
"""

# ruff: noqa: F811

from __future__ import annotations

from typing import Any

import pytest

from arbora.ingest.service import IngestSyncReceipt
from tests.api._loopback_engine import Loopback, loopback  # noqa: F401
from tests.api._served_support import McpAsk, person
from tests.test_served_operation_api import served  # noqa: F401


class Runner:
    """The composed runner: one durable job per idempotency key."""

    def __init__(self) -> None:
        self.records: dict[str, Any] = {}
        self.applied: list[str] = []
        self.proposed = False

    def _once(self, key: str, make: Any) -> Any:
        return self.records.setdefault(key, make())

    async def sync_source(
        self, *, tenant, source_id, mode, idempotency_key, caller=None
    ):
        return self._once(
            idempotency_key,
            lambda: IngestSyncReceipt(
                job_id=f"job-{len(self.records) + 1}",
                source_id=source_id,
                tenant=tenant,
                mode=mode,
                status="queued",
            ),
        )

    async def propose_repair(self, *, tenant, drift_id, idempotency_key):
        self.proposed = True
        return {"repair_id": "r1", "preview": {"changes": 2}}

    async def get_repair(self, *, tenant, repair_id):
        preview = {"changes": 2} if self.proposed else None
        return {"repair_id": repair_id, "preview": preview}

    async def apply_repair(self, *, tenant, repair_id, approval_ref, idempotency_key):
        self.applied.append(approval_ref)
        return {"repair_id": repair_id, "applied": True, "approval_ref": approval_ref}

    async def reembed(self, *, tenant, source_id, model, idempotency_key):
        return self._once(
            idempotency_key,
            lambda: {"job_id": f"job-{len(self.records) + 1}", "status": "queued"},
        )


RUNNER = Runner()
STATE: dict[str, Any] = {"compose": True}


def loopback_bindings() -> dict[str, Any]:
    return {"ingest_runner": RUNNER} if STATE["compose"] else {}


@pytest.fixture(autouse=True)
def _runner(request: pytest.FixtureRequest) -> None:
    RUNNER.records.clear()
    RUNNER.applied.clear()
    RUNNER.proposed = False
    STATE["compose"] = "absent_runner" not in request.node.name


def _mcp(loop: Loopback, monkeypatch: pytest.MonkeyPatch) -> McpAsk:
    return McpAsk(
        loop.invocation, monkeypatch, person("one", extra_scopes=("ingest:write",))
    )


@pytest.mark.spec("ARBORA-OPS-R020")
def test_sync_returns_durable_receipt_over_both_transports(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    loopback.serve({}, "ingest:write")
    params = {"source_id": "wiki", "mode": "incremental"}
    first = Loopback.value(loopback.call("ingest.sources.sync", params, "sync-1"))
    again = Loopback.value(loopback.call("ingest.sources.sync", params, "sync-1"))
    assert first["job_id"] == again["job_id"] and first["status"] == "queued"
    over_mcp = _mcp(loopback, monkeypatch)(
        "act", "ingest.sources.sync", params, idempotency_key="sync-1", execute=True
    )
    assert first["job_id"] in str(over_mcp)
    assert len(RUNNER.records) == 1


@pytest.mark.spec("ARBORA-OPS-R020")
def test_absent_runner_fails_closed(loopback: Loopback) -> None:
    loopback.serve({}, "ingest:write")
    reply = loopback.call(
        "ingest.sources.sync", {"source_id": "wiki", "mode": "full"}, "sync-2"
    )
    assert reply.json()["error"]["code"] == "UNAVAILABLE", reply.text
    assert RUNNER.records == {}


@pytest.mark.spec("ARBORA-OPS-R020.4")
def test_drift_repair_previews_then_applies_with_approval(loopback: Loopback) -> None:
    loopback.serve({}, "ingest:write")
    early = loopback.call(
        "ingest.drift.repair_approve",
        {"repair_id": "r1", "approval_ref": "appr-1"},
        "ap-0",
    )
    assert early.status_code >= 400 and RUNNER.applied == []
    proposed = Loopback.value(
        loopback.call("ingest.drift.repair_propose", {"drift_id": "d1"}, "pr-1")
    )
    assert proposed["preview"] == {"changes": 2}
    assert RUNNER.applied == []
    done = Loopback.value(
        loopback.call(
            "ingest.drift.repair_approve",
            {"repair_id": "r1", "approval_ref": "appr-1"},
            "ap-1",
        )
    )
    assert done["applied"] is True and RUNNER.applied == ["appr-1"]


@pytest.mark.spec("ARBORA-OPS-R020.5")
def test_reembed_creates_one_durable_job_per_key(loopback: Loopback) -> None:
    loopback.serve({}, "ingest:write")
    params = {"source_id": "wiki", "model": "embed-1"}
    one = Loopback.value(loopback.call("ingest.embedding.reembed", params, "re-1"))
    same = Loopback.value(loopback.call("ingest.embedding.reembed", params, "re-1"))
    other = Loopback.value(loopback.call("ingest.embedding.reembed", params, "re-2"))
    assert one["job_id"] == same["job_id"] != other["job_id"]
