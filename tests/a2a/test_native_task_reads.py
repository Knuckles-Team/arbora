"""Baseline A2A reads use native caller views and existing durable run output."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from tests.a2a.test_context_continuation import SESSION, row

pytestmark = pytest.mark.spec("ARBORA-A2A-R001")
TASK = "a2a-" + "b" * 64


def record() -> dict[str, Any]:
    return {**row(), "work_item_id": "workitem:orchestrator:" + TASK}


def fixture(monkeypatch: Any, pages: list[Any]) -> tuple[Any, list[Any]]:
    calls: list[Any] = []

    def read(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return pages.pop(0)

    engine = SimpleNamespace(work_items=SimpleNamespace(list=read))
    authority = WorkItemA2AAuthority(lambda: engine)
    monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
    monkeypatch.setattr(authority, "_owner_ref", lambda session: "owner")
    return authority, calls


@pytest.mark.asyncio
async def test_native_list_preserves_empty_page_and_opaque_cursor(
    monkeypatch: Any,
) -> None:
    authority, calls = fixture(
        monkeypatch,
        [
            {"items": [], "next_cursor": "native.opaque/cursor"},
            {"items": [record()], "next_cursor": None},
        ],
    )
    tasks, cursor = await authority.list(cursor=None, limit=1)
    assert tasks == [] and cursor is not None
    tasks, cursor = await authority.list(cursor=cursor, limit=1)
    assert [task.id for task in tasks] == [TASK]
    assert tasks[0].status.timestamp == "1970-01-01T00:00:00.001000+00:00"
    assert cursor is None
    assert calls[1]["cursor"] == "native.opaque/cursor"
    assert calls[0] == {
        "tenant": SESSION.tenant,
        "kind": "orchestrator_task",
        "metadata_match": {
            "a2a_schema": "arbora-a2a-unary-v1",
            "a2a_owner_ref": "owner",
        },
        "cursor": None,
        "limit": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "schema", "kind", "identity"])
async def test_native_list_refuses_foreign_rows(monkeypatch: Any, change: str) -> None:
    item = record()
    if change == "owner":
        item["metadata"]["a2a_owner_ref"] = "foreign"
    elif change == "schema":
        item["metadata"]["a2a_schema"] = "foreign"
    elif change == "kind":
        item["kind"] = "foreign"
    else:
        item["work_item_id"] = TASK
    authority, _ = fixture(monkeypatch, [{"items": [item], "next_cursor": None}])
    with pytest.raises((RuntimeError, ValueError)):
        await authority.list(cursor=None, limit=1)


@pytest.mark.asyncio
async def test_cursor_is_bound_to_owner_and_tenant(monkeypatch: Any) -> None:
    authority, calls = fixture(monkeypatch, [])
    for owner, tenant in [("foreign", SESSION.tenant), ("owner", "foreign")]:
        cursor = authority._cursor(owner, tenant, "opaque")
        with pytest.raises(ValueError, match="cursor"):
            await authority.list(cursor=cursor, limit=1)
    assert calls == []


def payload() -> str:
    return json.dumps(
        {
            "type": "graph_complete",
            "run_id": TASK,
            "result": {
                "run_id": TASK,
                "status": "completed",
                "output": "persisted reply",
            },
        }
    )


@pytest.mark.asyncio
async def test_get_reads_durable_reply_on_fresh_authority(monkeypatch: Any) -> None:
    queries: list[Any] = []
    engine = SimpleNamespace(
        query_cypher=lambda query, params: (
            queries.append((query, params)) or [{"payload": payload()}]
        )
    )
    for _ in range(2):
        authority = WorkItemA2AAuthority(lambda: engine)
        monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
        monkeypatch.setattr(authority, "_authorized_item", lambda *args: record())
        task = await authority.get(TASK)
        assert task is not None and task.artifacts is not None
        assert task.artifacts[0].parts[0].text == "persisted reply"
    assert len(queries) == 2
    assert "MATCH (e) WHERE e.type = 'RunEvent'" in queries[0][0]
    assert queries[0][1] == {"run_id": TASK, "event_type": "graph_complete"}


@pytest.mark.parametrize(
    "rows",
    [
        [{"payload": "truncated{"}],
        [{"payload": payload()}] * 2,
        [{"payload": payload().replace(TASK, "foreign")}],
    ],
)
def test_invalid_persisted_reply_is_not_reconstructed(rows: Any) -> None:
    authority = WorkItemA2AAuthority(
        lambda: SimpleNamespace(query_cypher=lambda *args: rows)
    )
    with pytest.raises(RuntimeError, match="reply is unavailable"):
        authority._persisted_artifacts(TASK, item=record())


@pytest.mark.asyncio
async def test_foreign_task_never_reads_run_events(monkeypatch: Any) -> None:
    authority = WorkItemA2AAuthority(lambda: pytest.fail("engine accessed"))
    monkeypatch.setattr(authority, "_session", lambda scope: SESSION)
    monkeypatch.setattr(authority, "_authorized_item", lambda *args: None)
    assert await authority.get(TASK) is None


@pytest.mark.asyncio
async def test_changed_listing_caller_refuses_rows(monkeypatch: Any) -> None:
    authority, _ = fixture(monkeypatch, [])

    def read(**kwargs: Any) -> Any:
        monkeypatch.setattr(authority, "_session", lambda scope: object())
        return {"items": [record()], "next_cursor": None}

    monkeypatch.setattr(authority, "_list_rows", read)
    with pytest.raises(PermissionError, match="caller changed"):
        await authority.list(cursor=None, limit=1)


def test_missing_run_event_does_not_use_process_cache() -> None:
    from arbora.a2a.authority import remember_task_output

    remember_task_output(TASK, "not durable evidence")
    authority = WorkItemA2AAuthority(
        lambda: SimpleNamespace(query_cypher=lambda *args: [])
    )
    assert authority._persisted_artifacts(TASK, item=record()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "page",
    [
        [],
        {"items": {}},
        {"items": [record(), record()]},
        {"items": [], "next_cursor": ""},
        {"items": [], "next_cursor": 3},
    ],
)
async def test_invalid_native_page_refuses(monkeypatch: Any, page: Any) -> None:
    authority, _ = fixture(monkeypatch, [page])
    with pytest.raises(RuntimeError, match="invalid data"):
        await authority.list(cursor=None, limit=1)
