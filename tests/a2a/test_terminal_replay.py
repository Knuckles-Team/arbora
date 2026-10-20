"""Terminal replay consumer isolation; producer fixtures are not live acceptance."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import Request

from arbora.a2a.application import _resubscribe
from arbora.a2a.models import A2ATask, A2ATaskStatus
from arbora.a2a.terminal_event import A2ATerminalEventUnavailable
from arbora.a2a.terminal_replay import project_replay
from tests.a2a.test_terminal_event import OWNER, SESSION, TASK, record

CURSOR = "eg-terminal-v1:" + "1" * 64
ITEM = "workitem:orchestrator:" + TASK


def replay():
    raw = record()
    event = {**raw["terminal_event"], "work_item_id": ITEM, "replay_cursor": CURSOR}
    return {"work_item": raw["work_item"], "events": [event], "next_cursor": CURSOR}


def project(value, cursor=None):
    return project_replay(
        value,
        session=SESSION,
        task_id=TASK,
        owner_ref=OWNER,
        work_item_id=ITEM,
        run_id=TASK,
        after_cursor=cursor,
    )


def test_committed_replay_then_acknowledgement():
    item, event, cursor = project(replay())
    assert event["replay_cursor"] == cursor == CURSOR
    empty = {"work_item": item, "events": [], "next_cursor": cursor}
    assert project(empty, cursor) == (item, None, cursor)
    with pytest.raises(A2ATerminalEventUnavailable):
        project(empty)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v["work_item"]["metadata"].update(a2a_owner_ref="foreign"),
        lambda v: v["work_item"].update(work_item_id="foreign"),
        lambda v: v["events"][0].update(run_id="foreign"),
        lambda v: v["events"][0].update(tenant="foreign"),
        lambda v: v["events"][0].update(replay_cursor="foreign"),
        lambda v: v["events"][0].update(work_item_id="foreign"),
        lambda v: v.update(next_cursor="injected\ndata: bad"),
        lambda v: v["events"].append(deepcopy(v["events"][0])),
    ],
)
def test_cross_bound_and_malformed_replay_refused(mutate):
    value = replay()
    mutate(value)
    with pytest.raises(A2ATerminalEventUnavailable):
        project(value)


def test_empty_replay_still_checks_owner():
    value = replay()
    value["events"] = []
    value["work_item"]["metadata"]["a2a_owner_ref"] = "foreign"
    with pytest.raises(A2ATerminalEventUnavailable):
        project(value, CURSOR)


@pytest.mark.asyncio
async def test_sse_replay_and_acknowledged_cursor_do_not_send():
    calls = []
    task = A2ATask(
        id=TASK,
        context_id="a2a-context-" + "b" * 64,
        status=A2ATaskStatus(state="completed"),
    )

    async def read(task_id, cursor):
        calls.append((task_id, cursor))
        return (None if cursor else task), CURSOR

    service = SimpleNamespace(replay_terminal=read)
    request = Request({"type": "http", "headers": []})
    response = await _resubscribe(service, request, {"id": TASK}, 7)
    assert response.media_type == "text/event-stream"
    assert response.body.startswith(f"id: {CURSOR}\ndata: ".encode())
    request = Request(
        {"type": "http", "headers": [(b"last-event-id", CURSOR.encode())]}
    )
    response = await _resubscribe(service, request, {"id": TASK}, 8)
    assert response.body == b""
    assert calls == [(TASK, None), (TASK, CURSOR)]
    with pytest.raises(ValueError):
        await _resubscribe(service, request, {"id": TASK, "cursor": "other"}, 9)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_authority_uses_native_reader_and_rechecks_caller(monkeypatch):
    from arbora.a2a.authority import WorkItemA2AAuthority

    value = replay()
    value["work_item"]["metadata"]["a2a_context_id"] = "a2a-context-" + "b" * 64
    authority = WorkItemA2AAuthority(lambda: object())
    monkeypatch.setattr(authority, "_authorized_item", lambda *_: value["work_item"])
    monkeypatch.setattr(authority, "_session", lambda _: SESSION)
    monkeypatch.setattr(authority, "_owner_ref", lambda _: OWNER)
    calls = []

    async def read(**kwargs):
        calls.append(kwargs)
        return value

    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda _: SimpleNamespace(
                async_client=SimpleNamespace(
                    work_items=SimpleNamespace(replay_terminal=read)
                )
            )
        )
    )
    monkeypatch.setattr(authority, "_engine", lambda: (engine, engine))
    task, cursor = await authority.replay_terminal(TASK, None)
    assert task.metadata["arbora"]["terminalEvent"]["replay_cursor"] == cursor == CURSOR
    assert calls == [{"tenant": "tenant", "work_item_id": ITEM, "after_cursor": None}]

    async def changed(**kwargs):
        monkeypatch.setattr(
            authority, "_session", lambda _: SimpleNamespace(tenant="foreign")
        )
        return value

    engine.graph_compute.for_graph = lambda _: SimpleNamespace(
        async_client=SimpleNamespace(
            work_items=SimpleNamespace(replay_terminal=changed)
        )
    )
    with pytest.raises(PermissionError):
        await authority.replay_terminal(TASK, None)


@pytest.mark.asyncio
async def test_unauthorized_task_never_reads_producer(monkeypatch):
    from arbora.a2a.authority import WorkItemA2AAuthority

    authority = WorkItemA2AAuthority(lambda: object())
    monkeypatch.setattr(authority, "_session", lambda _: SESSION)
    monkeypatch.setattr(authority, "_owner_ref", lambda _: OWNER)
    monkeypatch.setattr(authority, "_authorized_item", lambda *_: None)
    monkeypatch.setattr(
        authority, "_engine", lambda: pytest.fail("unauthorized native read")
    )
    with pytest.raises(PermissionError):
        await authority.replay_terminal(TASK, None)


def test_authenticated_route_serves_sse_and_keeps_streaming_card_false():
    from fastapi.testclient import TestClient

    from tests.a2a.test_application import _app

    app, auth, authority = _app()
    calls = []

    async def read(task_id, cursor):
        calls.append((task_id, cursor))
        return authority.task, CURSOR

    authority.replay_terminal = read
    client = TestClient(app)
    response = client.post(
        "/a2a",
        headers={"Authorization": "Bearer verified"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tasks/resubscribe",
            "params": {"id": authority.task.id},
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert response.text.startswith(f"id: {CURSOR}\ndata: ")
    assert calls == [(authority.task.id, None)]
    assert auth.scopes == ["kg:read"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers,params",
    [
        ([], {"id": TASK, "cursor": ""}),
        ([(b"last-event-id", b"")], {"id": TASK}),
        ([(b"last-event-id", b"x" * 1025)], {"id": TASK}),
    ],
)
async def test_invalid_cursor_never_becomes_initial_replay(headers, params):
    request = Request({"type": "http", "headers": headers})
    with pytest.raises(ValueError):
        await _resubscribe(SimpleNamespace(), request, params, 1)
