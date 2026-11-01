"""Flat snapshot source qualification; no native services or child conversations."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from arbora.api.ops.run_graph import (
    GRAPH_SCHEMA,
    ExecutionGraphParams,
    declared_topology,
    project_graph,
    step_key,
)
from arbora.api.ops.run_replay import RunEventsParams, project_page
from arbora.api.ops.runs import handle_runs_execution_graph, operations
from arbora.mcp_server import run_reader
from arbora.mcp_server.run_reader import NativeRunReader
from tests.api.test_run_replay import event, reader_context, replay_engine, rows

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.8.1")
ROOT = "work-item:" + "a" * 64


def request(**changes):
    return ExecutionGraphParams(
        **{
            "run_id": "run-one",
            "task_id": ROOT,
            "schema_version": GRAPH_SCHEMA,
            **changes,
        }
    )


def native_plan(**changes):
    now = int(time.time() * 1000)
    return {
        "run_id": "run-one",
        "revision": 2,
        "observed_at_ms": now - 1000,
        "expires_at_ms": now + 60000,
        "items": [],
        "task_plan": {
            "plan_digest": "plan-one",
            "total_tasks": 4,
            "output_task_id": "join",
            "tasks": [
                {"task_id": name, "depends_on": deps, "prompt": "PRIVATE PROMPT"}
                for name, deps in (
                    ("first", []),
                    ("left", ["first"]),
                    ("right", ["first"]),
                    ("join", ["left", "right"]),
                )
            ],
        },
        **changes,
    }


def step_event(sequence=2, **changes):
    return event(
        sequence,
        **{
            "type": "task_started",
            "status": "running",
            "task_id": "first",
            "parent_run_id": "run-one",
            "plan_id": "plan-one",
            "depends_on": [],
            "output": "PRIVATE OUTPUT",
            "reasoning": "PRIVATE REASONING",
            **changes,
        },
    )


def graph(*events, selected=None):
    selected = selected or request()
    data = rows(*events)
    page = project_page(data, RunEventsParams(run_id=selected.run_id))
    return project_graph(selected, declared_topology(native_plan()), data, page)


def test_flat_fanout_join_is_prompt_free_and_never_a_child_conversation():
    result = graph(event(), step_event())
    assert result["task_id"] == ROOT and result["run_id"] == "run-one"
    snapshot = result["snapshot"]
    assert snapshot["topology_completeness"] == "complete"
    assert snapshot["lifecycle_completeness"] == "incomplete"
    assert snapshot["lifecycle_reason"] == "observed_prefix_only"
    assert snapshot["covered_after"] == 2
    assert [node["status"] for node in snapshot["nodes"]] == [
        "running",
        "unobserved",
        "unobserved",
        "unobserved",
    ]
    assert snapshot["nodes"][0]["evidence"] == {"event_id": "event-2", "sequence": 2}
    assert [(edge["source"], edge["target"]) for edge in snapshot["edges"]] == [
        (step_key("run-one", a), step_key("run-one", b))
        for a, b in (
            ("first", "left"),
            ("first", "right"),
            ("left", "join"),
            ("right", "join"),
        )
    ]
    assert {edge["kind"] for edge in snapshot["edges"]} == {"depends_on"}
    for capability in (
        "can_read_transcript",
        "can_message_active",
        "can_continue_completed",
        "supports_spawned_children",
    ):
        assert snapshot[capability] is False
    encoded = json.dumps(result)
    assert (
        "PRIVATE" not in encoded and "prompt" not in encoded and "output" not in encoded
    )


def test_stable_keys_are_run_qualified_and_delimiter_safe():
    assert step_key("first:run", "step") != step_key("first", "run:step")
    one = graph(event())["snapshot"]["nodes"][0]["key"]
    two = graph(event(run_id="run-two"), selected=request(run_id="run-two"))[
        "snapshot"
    ]["nodes"][0]["key"]
    assert one != two
    assert one == graph(event())["snapshot"]["nodes"][0]["key"]


def test_terminal_root_and_no_more_do_not_complete_unobserved_steps():
    result = graph(event(), event(2, type="graph_complete"))["snapshot"]
    assert (
        result["has_more"] is False and result["lifecycle_completeness"] == "incomplete"
    )
    assert {node["status"] for node in result["nodes"]} == {"unobserved"}


@pytest.mark.parametrize(
    "changes",
    [
        {"plan_id": "another-plan"},
        {"task_id": "unknown"},
        {"parent_run_id": "other-run"},
        {"depends_on": ["unknown"]},
        {"status": "completed"},
    ],
)
def test_lifecycle_binding_conflicts_are_not_reinterpreted(changes):
    with pytest.raises(ValueError):
        graph(event(), step_event(**changes))


@pytest.mark.parametrize(
    "kind,status",
    [
        ("task_completed", "completed"),
        ("task_failed", "failed"),
        ("task_cancelled", "cancelled"),
    ],
)
def test_only_ordered_evidence_advances_lifecycle(kind, status):
    value = graph(event(), step_event(), step_event(3, type=kind, status=status))
    assert value["snapshot"]["nodes"][0]["status"] == status
    with pytest.raises(ValueError):
        graph(event(), step_event(type=kind, status=status))
    with pytest.raises(ValueError):
        graph(event(), step_event(), step_event(3))


def test_blocked_is_evidenced_without_inventing_start():
    value = graph(event(), step_event(type="task_blocked", status="blocked"))
    assert value["snapshot"]["nodes"][0]["status"] == "blocked"


@pytest.mark.parametrize(
    "change", ["duplicate", "foreign", "cycle", "oversize", "missing", "count"]
)
def test_malformed_declared_topology_fails_closed(change):
    raw = native_plan()
    plan = raw["task_plan"]
    if change == "duplicate":
        plan["tasks"][1]["task_id"] = "first"
    elif change == "foreign":
        plan["tasks"][1]["depends_on"] = ["foreign"]
    elif change == "cycle":
        plan["tasks"][0]["depends_on"] = ["join"]
    elif change == "oversize":
        plan["tasks"] *= 17
    elif change == "missing":
        plan["tasks"] = []
    else:
        plan["total_tasks"] = 3
    with pytest.raises(ValueError):
        declared_topology(raw)


@pytest.mark.parametrize(
    "change",
    [{"schema_version": "v2"}, {"schema_version": None}, {"tenant": "foreign"}],
)
def test_schema_negotiation_and_authority_fields_refuse(change):
    with pytest.raises(ValidationError):
        request(**change)
    with pytest.raises(ValidationError):
        ExecutionGraphParams(run_id="run-one")


def installed_reader(monkeypatch, events, *, owner=True):
    reader = NativeRunReader(lambda: replay_engine(*events))
    calls = []

    def owned(task_id, scope):
        calls.append(("owner", task_id, scope))
        return (
            {"status": "succeeded", "metadata": {"a2a_run_id": "run-one"}}
            if owner
            else None
        )

    async def native(context, run_id):
        calls.append(("plan", run_id))
        return native_plan()

    monkeypatch.setattr(reader._authority, "_authorized_item", owned)
    monkeypatch.setattr(run_reader, "read_native_snapshot", native)
    return reader, calls


@pytest.mark.parametrize("owner,run_id", [(False, "run-one"), (True, "foreign")])
async def test_owner_and_exact_root_run_binding_precede_all_protected_reads(
    monkeypatch, owner, run_id
):
    reader, calls = installed_reader(monkeypatch, [event()], owner=owner)
    value = await reader.execution_graph(
        context=reader_context(), request=request(run_id=run_id)
    )
    assert value["availability"] == "unavailable" and value["snapshot"] is None
    assert calls == [("owner", ROOT, "kg:read")]


@pytest.mark.parametrize("change", ["digest", "tasks", "expired", "foreign", "missing"])
async def test_plan_recheck_rejects_mutation_or_expiry(monkeypatch, change):
    reader, _ = installed_reader(monkeypatch, [event(), step_event()])
    count = 0

    async def native(context, run_id):
        nonlocal count
        count += 1
        raw = native_plan()
        if count == 2:
            if change == "digest":
                raw["task_plan"]["plan_digest"] = "new-plan"
            elif change == "tasks":
                raw["task_plan"]["tasks"][0]["prompt"] = "changed private input"
            elif change == "expired":
                raw["expires_at_ms"] = 1
            elif change == "foreign":
                raw["run_id"] = "foreign"
            else:
                return None
        return raw

    monkeypatch.setattr(run_reader, "read_native_snapshot", native)
    value = await reader.execution_graph(context=reader_context(), request=request())
    assert value["availability"] == "unavailable" and count == 2


@pytest.mark.parametrize(
    "events",
    [
        [],
        [event(2)],
        [event(), event(3)],
        [event(), event(2, event_id="event-1")],
        [event(), event(2, type="graph_complete"), event(3)],
        [event(), event(2, type="tool_call")],
        [event(), step_event(plan_id="other")],
        [event(), event(2, run_id="other")],
        [event(prompt="x" * 1_048_576)],
    ],
)
async def test_bad_or_unordered_history_remains_unavailable_without_filtering(
    monkeypatch, events
):
    reader, _ = installed_reader(monkeypatch, events)
    value = await reader.execution_graph(context=reader_context(), request=request())
    assert value["availability"] == "unavailable" and value["snapshot"] is None


async def test_event_prefix_is_bounded_without_claiming_completion(monkeypatch):
    events = [event(n) for n in range(1, 1003)]
    reader, _ = installed_reader(monkeypatch, events)
    value = await reader.execution_graph(context=reader_context(), request=request())
    snapshot = value["snapshot"]
    assert snapshot["covered_after"] == 1000 and snapshot["has_more"] is True
    assert snapshot["lifecycle_completeness"] == "incomplete"


async def test_unavailable_reader_and_unsupported_schema_before_reads():
    op = next(item for item in operations() if item.id == "runs.execution_graph")
    result = await handle_runs_execution_graph(
        SimpleNamespace(services={}), request().model_dump(), op
    )
    assert result["value"]["availability"] == "unavailable"


@pytest.mark.parametrize("allowed", [True, False])
async def test_http_and_mcp_share_native_owner_checked_projection(monkeypatch, allowed):
    from arbora.api.http.app import create_api_application
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.mcp.caller import caller_from_session
    from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
    from arbora.api.registry import Registry
    from tests.api.test_invoke_contract import setup_services

    op = next(item for item in operations() if item.id == "runs.execution_graph")
    services, runtime, *_ = setup_services(op, registry=Registry([op]))
    context = reader_context()
    caller = context.caller
    if not allowed:
        caller = caller_from_session(
            replace(caller.session, scopes=frozenset()), credential_kind="bearer"
        )
    reader, calls = installed_reader(monkeypatch, [event(), step_event()])

    async def dispatch(operation, params, execution):
        return await handle_runs_execution_graph(
            replace(
                context, caller=execution.caller, services={"run_trace_reader": reader}
            ),
            params,
            operation,
        )

    async def authenticate(request):
        return caller

    async def visible(operation, verified):
        return True

    runtime.dispatch = dispatch
    auth = AmbientHTTPAuthenticator(session_for_request=lambda _: caller.session)
    auth.authenticate = authenticate
    app = create_api_application(
        services=services, authenticator=auth, invoke=invoke, visibility=visible
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://fixture"
    ) as client:
        response = await client.post(
            "/ops/runs.execution_graph", json=request().model_dump()
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
        "ask", projection, op=op.id, params=request().model_dump()
    )
    if allowed:
        assert response.status_code == 200, response.text
        assert response.json()["result"] == mcp["result"]
        assert mcp["result"]["value"]["snapshot"]["nodes"][0]["status"] == "running"
        assert calls[0] == ("owner", ROOT, "kg:read")
    else:
        assert response.status_code == 403 and not mcp["ok"]
        assert calls == []


def test_installed_native_contract_can_decode_the_topology_without_a_service():
    from medulla.generated import models

    raw = native_plan()
    raw["task_plan"].update(
        input_digest="input",
        max_concurrency=2,
        max_steps=4,
        policy_digest="policy",
        policy_revision="v1",
    )
    decoded = models.CurrentPlanSnapshot.model_validate(raw).model_dump(mode="json")
    assert declared_topology(decoded) == declared_topology(raw)


async def test_metadata_census_limit_remains_fail_closed(monkeypatch):
    reader, _ = installed_reader(monkeypatch, [event(n) for n in range(1, 10002)])
    value = await reader.execution_graph(context=reader_context(), request=request())
    assert value["availability"] == "unavailable"


async def test_native_refusal_is_preserved_not_disguised_as_missing(monkeypatch):
    from arbora.api.errors import EngineRefusal

    reader, _ = installed_reader(monkeypatch, [event()])

    async def denied(context, run_id):
        raise EngineRefusal("ACCESS_DENIED")

    monkeypatch.setattr(run_reader, "read_native_snapshot", denied)
    with pytest.raises(EngineRefusal) as error:
        await reader.execution_graph(context=reader_context(), request=request())
    assert error.value.code == "ACCESS_DENIED"


async def test_revoked_root_during_collection_prevents_second_plan_read(monkeypatch):
    reader, calls = installed_reader(monkeypatch, [event()])
    owners = iter([{"metadata": {"a2a_run_id": "run-one"}}, None])
    monkeypatch.setattr(
        reader._authority, "_authorized_item", lambda *args: next(owners)
    )
    value = await reader.execution_graph(context=reader_context(), request=request())
    assert value["availability"] == "unavailable"
    assert calls == [("plan", "run-one")]


@pytest.mark.parametrize("outcome", ["success", "failure", "cancelled"])
async def test_installed_au_runtime_events_match_flat_projection(monkeypatch, outcome):
    """Exercise the public runtime with a controlled local model, not event fixtures."""
    import asyncio

    from agent_utilities import host_ports
    from agent_utilities.runtime import (
        AgentRunExecutor,
        DependencyPlan,
        RunFailed,
        RunPlan,
        RunTask,
        StopRule,
    )
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    async def respond(messages, info):
        if outcome == "failure":
            raise RuntimeError("controlled failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError
        return ModelResponse(parts=[TextPart(content="PRIVATE OUTPUT")])

    tasks = native_plan()["task_plan"]["tasks"]
    plan = RunPlan(
        model=FunctionModel(respond),
        dependency_plan=DependencyPlan(
            tasks=tuple(
                RunTask(task["task_id"], task["prompt"], tuple(task["depends_on"]))
                for task in tasks
            ),
            output_task_id="join",
            max_concurrency=2,
            plan_id="plan-one",
        ),
        stop=StopRule(max_steps=4),
    )
    emitted = []
    monkeypatch.setattr(
        host_ports, "run_decision_provider", lambda: lambda request: plan
    )
    monkeypatch.setattr(
        host_ports, "emit_run_event", lambda value: emitted.append(dict(value))
    )
    executor = AgentRunExecutor()
    run_request = SimpleNamespace(task="controlled task", run_id="run-one")
    if outcome == "failure":
        with pytest.raises(RunFailed):
            await executor.execute_agent(run_request)
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await executor.execute_agent(run_request)
    else:
        result = await executor.execute_agent(run_request)
        assert result.status == "completed"
    reader, _ = installed_reader(monkeypatch, emitted)
    value = await reader.execution_graph(context=reader_context(), request=request())
    assert value["availability"] == "available"
    states = [node["status"] for node in value["snapshot"]["nodes"]]
    if outcome == "success":
        assert states == ["completed"] * 4
    else:
        assert states == [
            "failed" if outcome == "failure" else "cancelled",
            "blocked",
            "blocked",
            "blocked",
        ]
    assert "PRIVATE" not in json.dumps(value)
    assert value["snapshot"]["lifecycle_completeness"] == "incomplete"


def test_snapshot_output_bytes_are_bounded_even_for_dense_long_ids():
    ids = [str(n) + "x" * 500 for n in range(64)]
    raw = native_plan()
    raw["task_plan"].update(
        total_tasks=64,
        output_task_id=ids[-1],
        tasks=[
            {"task_id": key, "depends_on": ids[:index]} for index, key in enumerate(ids)
        ],
    )
    data = rows(event())
    page = project_page(data, RunEventsParams(run_id="run-one"))
    with pytest.raises(ValueError, match="byte bound"):
        project_graph(request(), declared_topology(raw), data, page)


async def test_lookahead_does_not_advance_the_covered_lifecycle(monkeypatch):
    events = [event(n) for n in range(1, 1001)] + [step_event(1001)]
    reader, _ = installed_reader(monkeypatch, events)
    value = await reader.execution_graph(context=reader_context(), request=request())
    snapshot = value["snapshot"]
    assert snapshot["covered_after"] == 1000 and snapshot["has_more"] is True
    assert snapshot["nodes"][0]["status"] == "unobserved"
    assert snapshot["nodes"][0]["evidence"] is None


def test_maximum_non_bmp_id_keys_fit_all_key_fields():
    run_id = "😀" * 512
    ids = ["🧪" * 512, "🚀" * 512]
    raw = native_plan(
        task_plan={
            "plan_digest": "unicode-plan",
            "total_tasks": 2,
            "output_task_id": ids[1],
            "tasks": [
                {"task_id": ids[0], "depends_on": []},
                {"task_id": ids[1], "depends_on": [ids[0]]},
            ],
        }
    )
    data = rows(event(run_id=run_id))
    page = project_page(data, RunEventsParams(run_id=run_id))
    value = project_graph(request(run_id=run_id), declared_topology(raw), data, page)
    snapshot = value["snapshot"]
    keys = [node["key"] for node in snapshot["nodes"]]
    assert all(8192 < len(key) <= 16384 for key in keys)
    assert [json.loads(key) for key in keys] == [
        [run_id, "plan_step", task] for task in ids
    ]
    assert snapshot["edges"] == [
        {"kind": "depends_on", "source": keys[0], "target": keys[1]}
    ]


def test_dense_graph_preflight_stops_before_whole_snapshot_serialization(monkeypatch):
    from arbora.api.ops import run_graph

    original = json.dumps
    largest_member = 0

    def bounded_dumps(value, *args, **kwargs):
        nonlocal largest_member
        assert not (isinstance(value, dict) and "snapshot" in value)
        encoded = original(value, *args, **kwargs)
        largest_member = max(largest_member, len(encoded))
        return encoded

    ids = [str(n) + "😀" * 500 for n in range(64)]
    raw = native_plan(
        task_plan={
            "plan_digest": "dense-plan",
            "total_tasks": 64,
            "output_task_id": ids[-1],
            "tasks": [
                {"task_id": key, "depends_on": ids[:index]}
                for index, key in enumerate(ids)
            ],
        }
    )
    data = rows(event())
    page = project_page(data, RunEventsParams(run_id="run-one"))
    monkeypatch.setattr(run_graph.json, "dumps", bounded_dumps)
    with pytest.raises(ValueError, match="byte bound"):
        project_graph(request(), declared_topology(raw), data, page)
    assert largest_member < 65536


@pytest.mark.parametrize(
    "initial,history",
    [
        ("unobserved", ()),
        ("running", ("running",)),
        ("completed", ("running", "completed")),
        ("failed", ("running", "failed")),
        ("cancelled", ("running", "cancelled")),
        ("blocked", ("blocked",)),
    ],
)
@pytest.mark.parametrize(
    "next_status", ["running", "completed", "failed", "cancelled", "blocked"]
)
def test_projection_lifecycle_transition_matrix(initial, history, next_status):
    kinds = {
        "running": "task_started",
        "completed": "task_completed",
        "failed": "task_failed",
        "cancelled": "task_cancelled",
        "blocked": "task_blocked",
    }
    prefix = [event()] + [
        step_event(sequence, type=kinds[status], status=status)
        for sequence, status in enumerate(history, start=2)
    ]
    assert graph(*prefix)["snapshot"]["nodes"][0]["status"] == initial
    following = step_event(len(prefix) + 1, type=kinds[next_status], status=next_status)
    allowed = {
        "unobserved": {"running", "blocked"},
        "running": {"completed", "failed", "cancelled"},
    }
    if next_status not in allowed.get(initial, set()):
        message = (
            "missing start evidence"
            if initial == "unobserved"
            else "conflicting lifecycle transition"
        )
        with pytest.raises(ValueError, match=message):
            graph(*prefix, following)
        return
    node = graph(*prefix, following)["snapshot"]["nodes"][0]
    assert node["status"] == next_status
    assert node["evidence"] == {
        "event_id": f"event-{len(prefix) + 1}",
        "sequence": len(prefix) + 1,
    }
