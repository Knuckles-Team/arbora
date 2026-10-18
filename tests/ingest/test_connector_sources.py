"""Connector sources through the composed SDK sync runner (ARBORA-OPS-R020.6.1-R020.6.5).

The served handlers (``arbora.ingest.service``) run over a real
:class:`ConnectorIngestRunner`, the real SDK runner and checkpoint store, and
the real WorkItem seam. Only the edges are fakes: the fleet gateway (one
declared connector), the engine KV and WorkItem methods, and the ingest submit.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.ingest import service
from arbora.ingest.connector_sources import (
    SOURCE_SYNC_KIND,
    ConnectorIngestRunner,
    FleetSourceSession,
)
from arbora.ingest.service import IngestSourceRecord

DECLARATION = {
    "connector": "fixture-mcp",
    "sources": [
        {
            "source_id": "items",
            "record_type": "FixtureItem",
            "tools": ["fixture_list_items"],
            "records_path": "data",
            "id_field": "id",
            "cursor_field": "updated",
            "incremental": True,
        }
    ],
}
ITEMS = [{"id": i, "updated": f"2026-01-0{i}"} for i in (1, 2, 3)]


class _Fleet:
    def __init__(self, *, fail_tool: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.fail_tool = fail_tool

    async def list(self, caller: Any, **params: Any) -> dict[str, Any]:
        return {
            "items": [
                {
                    "kind": "fleet",
                    "server": "fixture",
                    "name": "fixture_ingest_sources",
                },
                {"kind": "fleet", "server": "fixture", "name": "fixture_list_items"},
                {"kind": "fleet", "server": "other", "name": "other_echo"},
            ]
        }

    async def call(
        self,
        caller: Any,
        server: str,
        tool: str,
        arguments: Any,
        *,
        expected_effect: Any,
    ) -> Any:
        self.calls.append((server, tool, expected_effect))
        if tool == "fixture_ingest_sources":
            return {"structured_content": DECLARATION}
        if self.fail_tool:
            raise PermissionError("fleet call denied by policy")
        return {"data": ITEMS}


class _Engine:
    """Engine KV (``_send``) and WorkItem (``work_items``) edges."""

    def __init__(self) -> None:
        self.kv: dict[tuple[str, str, bool], tuple[bytes, int]] = {}
        self.items: dict[str, dict[str, Any]] = {}
        self.work_items = SimpleNamespace(
            submit=self._submit,
            get=lambda tenant, work_item_id: self.items.get(work_item_id),
            claim=self._claim,
            commit_result=self._commit,
        )

    async def _send(self, method: str, params: Any, graph: Any, **_: Any) -> Any:
        key = (params["namespace"], params["key"], bool(params.get("shared")))
        if method == "KvGet":
            row = self.kv.get(key)
            return None if row is None else {"value": row[0], "version": row[1]}
        version = self.kv.get(key, (b"", 0))[1]
        if params["expected_version"] != version:
            return {"swapped": False, "version": version}
        self.kv[key] = (params["new"], version + 1)
        return {"swapped": True, "version": version + 1}

    def _submit(self, request: Mapping[str, Any]) -> dict[str, Any]:
        for item_id, item in self.items.items():
            if item["idempotency_key"] == request["idempotency_key"]:
                return {"work_item_id": item_id, "created": False}
        item_id = f"wi-{len(self.items) + 1}"
        self.items[item_id] = {
            "kind": request["kind"],
            "idempotency_key": request["idempotency_key"],
            "status": "ready",
        }
        return {"work_item_id": item_id, "created": True}

    def _claim(self, request: Mapping[str, Any]) -> dict[str, Any]:
        self.items[request["work_item_id"]]["status"] = "leased"
        return {
            "claimed": True,
            "lease_holder_ref": "w",
            "lease_epoch": 1,
            "fencing_token": 1,
        }

    def _commit(self, *, work_item_id: str, outcome: str, **kw: Any) -> dict[str, Any]:
        self.items[work_item_id].update(status=outcome, error_ref=kw.get("error_ref"))
        return {"status": outcome}


class _Ingest:
    def __init__(self) -> None:
        self.batches: list[Any] = []

    async def submit(self, binding: Any, changes: Any) -> Any:
        self.batches.append((binding, changes))
        return SimpleNamespace(batch_digest=f"d{len(self.batches)}")


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> Any:
    session = SimpleNamespace(
        tenant="tenant-a",
        policy_version="p",
        engine_verified_context=lambda: {"tenant_id": "tenant-a"},
    )
    monkeypatch.setattr("arbora.work_items._session", lambda: session)
    engine, ingest = _Engine(), _Ingest()

    async def client(_tenant: str) -> Any:
        return engine

    def build(fleet: Any) -> ConnectorIngestRunner:
        return ConnectorIngestRunner(
            fleet=fleet,
            data_client=client,
            control_client=client,
            ingest_for=lambda _client: ingest,
        )

    return SimpleNamespace(engine=engine, ingest=ingest, build=build)


def _context(runner: Any, key: str | None = "idem-1") -> Any:
    caller = SimpleNamespace(tenant="tenant-a", principal="p", session=None)
    return SimpleNamespace(
        caller=caller, services={"ingest_runner": runner}, idempotency_key=key
    )


@pytest.mark.spec("ARBORA-OPS-R020.6.1")
def test_source_record_shape() -> None:
    record = IngestSourceRecord(
        source_id="fixture/items", tenant="t", state="active", connector="c", records=2
    )
    assert record.model_dump(exclude_none=True)["records"] == 2
    with pytest.raises(ValidationError):
        IngestSourceRecord(source_id="s", tenant="t", state="unknown")  # type: ignore[arg-type]


@pytest.mark.spec("ARBORA-OPS-R020.6.2")
async def test_list_sync_status_compose_runner(wired: Any) -> None:
    fleet = _Fleet()
    runner = wired.build(fleet)
    listed = await service.list_sources(_context(runner), {}, None)
    (source,) = listed["value"]["sources"]
    assert source["source_id"] == "fixture/items"
    assert source["record_type"] == "FixtureItem" and source["sync_state"] == "never"
    params = {"source_id": "fixture/items", "mode": "incremental"}
    receipt = (await service.sync_source(_context(runner), params, None))["value"]
    assert receipt["status"] == "completed"
    (_binding, changes) = wired.ingest.batches[0]
    assert [e.id for e in changes.entities] == ["1", "2", "3"]
    from arbora.api.registry import Effect

    assert all(effect is Effect.READ for _, _, effect in fleet.calls)
    status = (
        await service.get_source_status(
            _context(runner), {"source_id": "fixture/items"}, None
        )
    )["value"]
    assert status["records"] == 3 and status["cursor"] == "2026-01-03"
    assert status["state"] == "active" and status["sync_state"] == "completed"
    again = (await service.sync_source(_context(runner, "idem-2"), params, None))[
        "value"
    ]
    assert again["status"] == "completed" and len(wired.ingest.batches) == 1


@pytest.mark.spec("ARBORA-OPS-R020.6.3")
def test_served_invocation_binds_ingest_runner() -> None:
    from arbora.mcp_server.served_invocation import _with_ingest_runner

    fleet = object()
    bound = _with_ingest_runner({"fleet_gateway": fleet}, object(), "g", lambda t: t)
    assert bound is not None
    assert isinstance(bound["ingest_runner"], ConnectorIngestRunner)
    assert _with_ingest_runner({}, object(), "g", lambda t: t) == {}
    assert isinstance(FleetSourceSession(fleet, None, "s"), FleetSourceSession)


@pytest.mark.spec("ARBORA-OPS-R020.6.4")
async def test_refusals_fail_closed(wired: Any) -> None:
    runner = wired.build(_Fleet(fail_tool=True))
    for source_id in ("fixture/missing", "nope", "other/items"):
        with pytest.raises(OperationRefused) as refused:
            await service.get_source_status(
                _context(runner), {"source_id": source_id}, None
            )
        assert refused.value.code == "NOT_FOUND"
    with pytest.raises(OperationRefused) as unbound:
        await service.list_sources(SimpleNamespace(caller=None, services={}), {}, None)
    assert unbound.value.code == "UNAVAILABLE"
    params = {"source_id": "fixture/items", "mode": "full"}
    receipt = (await service.sync_source(_context(runner), params, None))["value"]
    assert receipt["status"] == "failed"
    (item,) = wired.engine.items.values()
    assert item["status"] == "failed" and item["kind"] == SOURCE_SYNC_KIND
    status = (
        await service.get_source_status(
            _context(runner), {"source_id": "fixture/items"}, None
        )
    )["value"]
    assert status["state"] == "error"


@pytest.mark.spec("ARBORA-OPS-R020.6.5")
async def test_receipt_binds_work_item(wired: Any) -> None:
    runner = wired.build(_Fleet())
    params = {"source_id": "fixture/items", "mode": "incremental"}
    receipt = (await service.sync_source(_context(runner), params, None))["value"]
    (item_id,) = wired.engine.items
    assert receipt["job_id"] == item_id
    assert wired.engine.items[item_id]["status"] == "succeeded"
    status = (
        await service.get_source_status(
            _context(runner), {"source_id": "fixture/items"}, None
        )
    )["value"]
    assert status["last_job_id"] == item_id
    assert "2026-01-01" not in str(receipt)
    job = await runner.get_job_status(tenant="tenant-a", job_id=item_id)
    assert job.status == "completed"
