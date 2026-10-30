"""ARBORA-HOST-R005.2.1.4/.5 and R005.2.3.2/.4: write-back and ingest actions resolve."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops.registry_factory import get_registry
from arbora.ingest import service
from arbora.ingest.connector_sources import SUBMISSION_KIND, ConnectorIngestRunner
from arbora.mcp_server.legacy_action_mapping import (
    APPROVED_ACTION_MAPPING,
    CODE_GRAPH_QUERY_ACTIONS,
    INGEST_SUBMISSION_ACTIONS,
)


def _resolves(actions: tuple[str, ...]) -> None:
    registry = get_registry()
    for action in actions:
        assert action in APPROVED_ACTION_MAPPING, action
        assert registry.get(APPROVED_ACTION_MAPPING[action]) is not None, action


@pytest.mark.spec("ARBORA-HOST-R005.2.1.4")
def test_process_writeback_resolves_to_engine_write_back() -> None:
    _resolves(("process_writeback",))
    assert APPROVED_ACTION_MAPPING["process_writeback"] == "eg.storage.WriteBack"


@pytest.mark.spec("ARBORA-HOST-R005.2.1.5")
def test_every_analyze_action_resolves_in_the_registry() -> None:
    _resolves(CODE_GRAPH_QUERY_ACTIONS + ("explain", "process_writeback"))


@pytest.mark.spec("ARBORA-HOST-R005.2.3.2")
def test_ingest_submission_actions_resolve_in_the_registry() -> None:
    _resolves(INGEST_SUBMISSION_ACTIONS)


@pytest.mark.spec("ARBORA-HOST-R005.2.3.4")
def test_job_actions_resolve_in_the_registry() -> None:
    _resolves(("jobs", "job_status", "cancel", "prioritize"))


class _WorkItems:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def submit(self, request: Any) -> dict[str, Any]:
        for item_id, item in self.items.items():
            if item["idempotency_key"] == request["idempotency_key"]:
                return {"work_item_id": item_id, "created": False}
        item_id = f"wi-{len(self.items) + 1}"
        self.items[item_id] = {
            "work_item_id": item_id,
            "kind": request["kind"],
            "idempotency_key": request["idempotency_key"],
            "status": "ready",
        }
        return {"work_item_id": item_id, "created": True}

    def get(self, tenant: str, work_item_id: str) -> Any:
        return self.items.get(work_item_id)

    def list(self, *, tenant: str, kind: str, cursor: Any, limit: int) -> Any:
        return {
            "items": [i for i in self.items.values() if i["kind"] == kind],
            "next_cursor": None,
        }

    def cancel(self, **kw: Any) -> dict[str, str]:
        self.items[kw["work_item_id"]]["status"] = "cancelled"
        return {"status": "cancelled"}

    def cas_metadata(self, **kw: Any) -> dict[str, Any]:
        self.calls.append(("cas", kw))
        return {"applied": True}


@pytest.fixture
def lifecycle(monkeypatch: pytest.MonkeyPatch) -> Any:
    session = SimpleNamespace(
        tenant="tenant-a",
        policy_version="p",
        engine_verified_context=lambda: {"tenant_id": "tenant-a"},
    )
    monkeypatch.setattr("arbora.work_items._session", lambda: session)
    engine = SimpleNamespace(work_items=_WorkItems())

    async def client(_tenant: str) -> Any:
        return engine

    runner = ConnectorIngestRunner(
        fleet=None, data_client=client, control_client=client
    )
    caller = SimpleNamespace(tenant="tenant-a", principal="p", session=None)
    context = SimpleNamespace(
        caller=caller, services={"ingest_runner": runner}, idempotency_key="k1"
    )
    return SimpleNamespace(engine=engine, context=context)


@pytest.mark.spec("ARBORA-HOST-R005.2.3.2")
@pytest.mark.spec("ARBORA-HOST-R005.2.3.4")
async def test_submit_list_prioritize_cancel_round_trip(lifecycle: Any) -> None:
    ctx, items = lifecycle.context, lifecycle.engine.work_items
    params = {"kind": "ingest_url", "target": "https://example.test/a", "options": {}}
    receipt = (await service.submit_ingest(ctx, params, None))["value"]
    assert receipt["status"] == "queued" and receipt["kind"] == "ingest_url"
    again = (await service.submit_ingest(ctx, params, None))["value"]
    assert again["job_id"] == receipt["job_id"]
    assert items.items[receipt["job_id"]]["kind"] == SUBMISSION_KIND

    listed = (await service.list_jobs(ctx, {"limit": 10}, None))["value"]
    assert [j["job_id"] for j in listed["jobs"]] == [receipt["job_id"]]

    ranked = (
        await service.prioritize_job(
            ctx, {"job_id": receipt["job_id"], "bucket": 1}, None
        )
    )["value"]
    assert ranked["applied"] is True and items.calls[0][1]["set_prio_bucket"] == 1

    cancelled = (await service.cancel_job(ctx, {"job_id": receipt["job_id"]}, None))[
        "value"
    ]
    assert cancelled["applied"] is True
    assert items.items[receipt["job_id"]]["status"] == "cancelled"
