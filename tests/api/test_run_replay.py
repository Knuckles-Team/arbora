"""Public replay contract, without external models or credentials."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from arbora.api.ops.run_replay import RunEventsParams, project_event, project_page
from arbora.api.ops.runs import handle_runs_events, operations

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.7")


def event(sequence=1, **changes):
    return {
        "run_id": "run-one",
        "event_id": f"event-{sequence}",
        "sequence": sequence,
        "type": "run_started",
        **changes,
    }


def rows(*events):
    return [{"payload": json.dumps(item)} for item in events]


def test_public_projection_omits_private_content_and_preserves_terminal():
    value = project_event(
        event(
            type="graph_complete",
            thought="private",
            reasoning="private",
            result={"output": "private"},
            prompt="private",
        ),
        run_id="run-one",
    )
    assert value["terminal"] is True
    assert value["type"] == "graph_complete"
    assert "private" not in json.dumps(value)
    assert value["category"] == "execution_trace"
    summary = project_event(
        event(type="public_summary", summary="Checked two sources"), run_id="run-one"
    )
    assert summary["category"] == "public_summary"
    assert summary["payload"] == {"summary": "Checked two sources"}


def test_bounded_exclusive_cursor():
    page = project_page(
        rows(event(2), event(3)), RunEventsParams(run_id="run-one", after=1, limit=1)
    )
    assert page["availability"] == "available"
    assert page["next_after"] == 2 and page["has_more"] is True
    assert len(page["events"]) == 1


@pytest.mark.parametrize(
    "events",
    [
        [event(2)],
        [event(1), event(3)],
        [event(1), event(1)],
        [event(1), event(2, event_id="event-1")],
        [event(type="graph_complete"), event(2)],
        [event(run_id="other")],
        [event(sequence=None)],
        [event(type="thought")],
    ],
)
def test_bad_or_legacy_history_is_unavailable(events):
    page = project_page(rows(*events), RunEventsParams(run_id="run-one"))
    assert page["availability"] == "unavailable" and page["events"] == []


@pytest.mark.parametrize(
    "values", [{"after": -1}, {"after": True}, {"limit": 1001}, {"tenant": "other"}]
)
def test_invalid_requests_refuse(values):
    with pytest.raises(ValidationError):
        RunEventsParams(run_id="run-one", **values)


async def test_operations_admit_read_scope_and_absent_reader():
    op = next(op for op in operations() if op.id == "runs.events")
    assert op.scopes == frozenset({"kg:read"})
    result = await handle_runs_events(
        SimpleNamespace(services={}), {"run_id": "run-one"}, op
    )
    assert result["value"]["availability"] == "unavailable"


async def test_owner_checked_before_event_query(monkeypatch):
    from arbora.mcp_server.run_reader import NativeRunReader

    calls = []
    engine = SimpleNamespace(query_cypher=lambda *args: calls.append(args))
    reader = NativeRunReader(lambda: engine)
    monkeypatch.setattr(reader._authority, "_authorized_item", lambda *args: None)
    result = await reader.events(
        context=reader_context(), request=RunEventsParams(run_id="run-one")
    )
    assert result["availability"] == "unavailable"
    assert calls == []


async def test_native_reader_cursor_anchor_and_restart_safe_projection(monkeypatch):
    from arbora.mcp_server.run_reader import NativeRunReader

    engine = replay_engine(event(1), event(2, type="graph_complete"))
    for _ in range(2):
        reader = NativeRunReader(lambda: engine)
        monkeypatch.setattr(
            reader._authority, "_authorized_item", lambda *args: {"status": "succeeded"}
        )
        result = await reader.events(
            context=reader_context(), request=RunEventsParams(run_id="run-one", after=1)
        )
        assert result["availability"] == "available"
        assert result["events"][0]["sequence"] == 2
        assert result["terminal"] is True


def reader_context():
    from dataclasses import replace

    from arbora.api.mcp.caller import caller_from_session
    from tests.a2a.test_operation_bridge import session
    from tests.mcp_server.test_current_plan_reader import context

    base = context()
    bound = replace(session(frozenset({"kg:read", "work:read"})), graph="control")
    caller = caller_from_session(bound, credential_kind="bearer")
    return replace(base, caller=caller, owner=caller.principal)


def test_child_task_identity_is_preserved():
    assert (
        project_event(event(task_id="child-task"), run_id="run-one")["task_id"]
        == "child-task"
    )


@pytest.mark.parametrize("extra", [False, True])
async def test_terminal_cursor_stays_terminal_or_refuses_later_events(
    monkeypatch, extra
):
    from arbora.mcp_server.run_reader import NativeRunReader

    engine = replay_engine(
        event(1), event(2, type="graph_complete"), *([event(3)] if extra else [])
    )
    reader = NativeRunReader(lambda: engine)
    monkeypatch.setattr(
        reader._authority, "_authorized_item", lambda *args: {"status": "succeeded"}
    )
    result = await reader.events(
        context=reader_context(), request=RunEventsParams(run_id="run-one", after=2)
    )
    assert result["availability"] == ("unavailable" if extra else "available")
    assert result["terminal"] is not extra
    assert result["events"] == []


async def test_permission_denial_is_not_unavailable(monkeypatch):
    from arbora.mcp_server.run_reader import NativeRunReader

    reader = NativeRunReader(lambda: None)

    def deny(*args):
        raise PermissionError("denied")

    monkeypatch.setattr(reader._authority, "_authorized_item", deny)
    with pytest.raises(PermissionError):
        await reader.events(
            context=reader_context(), request=RunEventsParams(run_id="run-one")
        )


@pytest.mark.parametrize(
    "status,expected",
    [("succeeded", "completed"), ("leased", "running"), ("dead_letter", "failed")],
)
async def test_get_maps_canonical_status(monkeypatch, status, expected):
    from arbora.mcp_server.run_reader import NativeRunReader

    reader = NativeRunReader(lambda: None)
    monkeypatch.setattr(
        reader._authority, "_authorized_item", lambda *args: {"status": status}
    )
    result = await reader.get(context=reader_context(), run_id="run-one")
    assert result["status"] == expected


@pytest.mark.parametrize("allowed", [True, False])
@pytest.mark.parametrize("task_id", [None, "work-item:" + "a" * 64])
async def test_http_and_mcp_use_same_registered_run_handler(
    monkeypatch, allowed, task_id
):
    from dataclasses import replace

    import httpx

    from arbora.api.http.app import create_run_compatibility_application
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
    from arbora.api.registry import Registry, contract_admission
    from tests.api.test_invoke_contract import setup_services

    op = next(item for item in operations() if item.id == "runs.events")
    services, runtime, *_ = setup_services(op, registry=Registry([op]))
    caller = reader_context().caller
    if not allowed:
        from arbora.api.mcp.caller import caller_from_session

        caller = caller_from_session(
            replace(caller.session, scopes=frozenset()), credential_kind="bearer"
        )
    calls = []

    class Reader:
        async def events(self, *, context, request):
            calls.append((context.caller.principal, request.task_id))
            return project_page(rows(event()), request)

    async def dispatch(operation, params, context):
        return await handle_runs_events(
            replace(context, services={"run_trace_reader": Reader()}), params, operation
        )

    async def resolve(request):
        return caller

    runtime.dispatch = dispatch
    monkeypatch.setattr(
        contract_admission, "validate_contract_admission", lambda _: None
    )
    auth = AmbientHTTPAuthenticator(session_for_request=lambda _: caller.session)
    auth.authenticate = resolve
    app = create_run_compatibility_application(
        services=services, authenticator=auth, invoke=invoke
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            "/run-one/events?after=0&limit=1"
            + (f"&task_id={task_id}" if task_id else "")
        )
    projection = MCPProjection(
        services.registry,
        services,
        SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
        lambda: caller,
        services.policy_gate,
        invoke,
    )
    mcp = await dispatch_verb(
        "ask",
        projection,
        op=op.id,
        params={"run_id": "run-one", "after": 0, "limit": 1, "task_id": task_id},
    )
    if allowed:
        assert response.status_code == 200, response.text
        assert mcp["ok"], mcp
        assert response.json() == mcp["result"]["value"]
        assert calls == [(caller.principal, task_id)] * 2
    else:
        assert response.status_code == 403, response.text
        assert not mcp["ok"]
        assert calls == []


def test_persistence_keeps_child_task_identity(monkeypatch):
    from arbora import agent_runs
    from tests.test_run_event_payloads import sink

    records = sink(monkeypatch)
    agent_runs.record_run_event(event(task_id="child-task"))
    assert records[0]["task_id"] == "child-task"


def replay_engine(*events):
    def query(query, params):
        if "AS event_id" in query:
            return [
                {
                    "sequence": e["sequence"],
                    "event_id": e["event_id"],
                    "event_type": e["type"],
                }
                for e in events
            ][: params["limit"]]
        return rows(*(e for e in events if e["sequence"] >= params["first"]))[
            : params["limit"]
        ]

    return SimpleNamespace(query_cypher=query)


@pytest.mark.parametrize(
    "history",
    [
        [event(1), event(2, type="graph_complete"), event(3), event(4)],
        [event(1), event(3), event(4)],
        [event(1), event(2, event_id="event-1"), event(3), event(4)],
        [event(1), event(2), event(2), event(3), event(4)],
    ],
)
async def test_cursor_cannot_bypass_invalid_earlier_history(monkeypatch, history):
    from arbora.mcp_server.run_reader import NativeRunReader

    reader = NativeRunReader(lambda: replay_engine(*history))
    monkeypatch.setattr(
        reader._authority, "_authorized_item", lambda *args: {"status": "running"}
    )
    result = await reader.events(
        context=reader_context(), request=RunEventsParams(run_id="run-one", after=3)
    )
    assert result["availability"] == "unavailable"


def test_metadata_census_bound():
    from arbora.api.ops.run_replay import MAX_HISTORY_EVENTS, validate_history

    history = [
        {"sequence": i, "event_id": str(i), "event_type": "run_started"}
        for i in range(1, MAX_HISTORY_EVENTS + 2)
    ]
    assert validate_history(history) is None
    assert validate_history(history[:-1]) is not None


@pytest.mark.parametrize("failure_stage", ["owner", "census", "page"])
@pytest.mark.parametrize("task_id", [None, "work-item:" + "a" * 64])
async def test_native_refusals_preserved_on_http_and_mcp(
    monkeypatch, failure_stage, task_id
):
    from dataclasses import replace

    import httpx
    from epistemic_graph import EngineResponseError

    from arbora.api.http.app import create_run_compatibility_application
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
    from arbora.api.registry import Registry, contract_admission
    from arbora.mcp_server.run_reader import NativeRunReader
    from tests.api.test_invoke_contract import setup_services

    op = next(item for item in operations() if item.id == "runs.events")
    services, runtime, *_ = setup_services(op, registry=Registry([op]))
    caller = reader_context().caller
    good = replay_engine(event())

    def query(query, params):
        stage = "census" if "AS event_id" in query else "page"
        if failure_stage == stage:
            raise EngineResponseError("ACCESS_DENIED", "private backend detail")
        return good.query_cypher(query, params)

    reader = NativeRunReader(lambda: SimpleNamespace(query_cypher=query))

    def owned(*args):
        if failure_stage == "owner":
            raise EngineResponseError("ACCESS_DENIED", "private owner detail")
        return {"status": "running", "metadata": {"a2a_run_id": "run-one"}}

    monkeypatch.setattr(reader._authority, "_authorized_item", owned)

    async def dispatch(operation, params, context):
        return await handle_runs_events(
            replace(context, services={"run_trace_reader": reader}), params, operation
        )

    async def resolve(request):
        return caller

    runtime.dispatch = dispatch
    monkeypatch.setattr(
        contract_admission, "validate_contract_admission", lambda _: None
    )
    auth = AmbientHTTPAuthenticator(session_for_request=lambda _: caller.session)
    auth.authenticate = resolve
    app = create_run_compatibility_application(
        services=services, authenticator=auth, invoke=invoke
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.get(
            "/run-one/events" + (f"?task_id={task_id}" if task_id else "")
        )
    mcp = await dispatch_verb(
        "ask",
        MCPProjection(
            services.registry,
            services,
            SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
            lambda: caller,
            services.policy_gate,
            invoke,
        ),
        op=op.id,
        params={"run_id": "run-one", "task_id": task_id},
    )
    assert response.status_code == 403, response.text
    assert not mcp["ok"] and mcp["error"]["code"] == "ACCESS_DENIED", mcp
    assert "private" not in response.text and "private" not in json.dumps(mcp)


@pytest.mark.parametrize(
    "metadata,task_id,available",
    [
        ({"a2a_run_id": "run-one"}, "work-item:" + "a" * 64, True),
        ({"a2a_run_id": "other-run"}, "work-item:" + "a" * 64, False),
        ({"a2a_run_id": None}, "run-one", False),
        ({}, "work-item:" + "a" * 64, False),
        ({}, "run-one", True),
    ],
)
async def test_root_task_run_pair_is_bound_before_payload(
    monkeypatch, metadata, task_id, available
):
    from arbora.mcp_server.run_reader import NativeRunReader

    queries = []
    engine = replay_engine(event(task_id="child-task"))

    def query(*args):
        queries.append(args)
        return engine.query_cypher(*args)

    reader = NativeRunReader(lambda: SimpleNamespace(query_cypher=query))
    admissions = []

    def owned(identity, scope):
        admissions.append((identity, scope))
        return {"status": "running", "metadata": metadata}

    monkeypatch.setattr(reader._authority, "_authorized_item", owned)
    result = await reader.events(
        context=reader_context(),
        request=RunEventsParams(run_id="run-one", task_id=task_id),
    )
    info = await reader.get(context=reader_context(), run_id="run-one", task_id=task_id)
    assert admissions == [(task_id, "kg:read")] * 2
    assert (result["availability"] == "available") is available
    assert (info["availability"] == "available") is available
    if available:
        assert result["events"][0]["task_id"] == "child-task"
        assert info["task_id"] == task_id
    else:
        assert queries == []


@pytest.mark.parametrize("foreign", ["owner", "tenant"])
async def test_foreign_root_is_unavailable_before_payload(monkeypatch, foreign):
    from arbora.a2a.authority import _A2A_METADATA_SCHEMA
    from arbora.mcp_server.run_reader import NativeRunReader

    context = reader_context()
    reader = NativeRunReader(lambda: None)
    owner = reader._authority._owner_ref(context.caller.session)
    item = {
        "kind": "orchestrator_task",
        "status": "running",
        "metadata": {
            "a2a_schema": _A2A_METADATA_SCHEMA,
            "a2a_owner_ref": "foreign" if foreign == "owner" else owner,
            "a2a_run_id": "run-one",
        },
    }

    def owned(identity, scope):
        # A cross-tenant get has no result; owner filtering is shared authority.
        candidate = None if foreign == "tenant" else item
        return candidate if reader._authority._owned(candidate, owner) else None

    monkeypatch.setattr(reader._authority, "_authorized_item", owned)
    result = await reader.events(
        context=context,
        request=RunEventsParams(run_id="run-one", task_id="work-item:" + "a" * 64),
    )
    assert result["availability"] == "unavailable"


async def test_raw_payload_budget_stops_before_next_batch(monkeypatch):
    from arbora.mcp_server.run_reader import NativeRunReader

    engine = replay_engine(*(event(i, private="x" * 70000) for i in range(1, 34)))
    calls = []

    def query(statement, params):
        calls.append(params)
        return engine.query_cypher(statement, params)

    reader = NativeRunReader(lambda: SimpleNamespace(query_cypher=query))
    monkeypatch.setattr(
        reader._authority, "_authorized_item", lambda *args: {"status": "running"}
    )
    result = await reader.events(
        context=reader_context(), request=RunEventsParams(run_id="run-one")
    )
    assert result["availability"] == "unavailable"
    assert [call["limit"] for call in calls] == [10001, 16]
