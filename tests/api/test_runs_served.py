"""Run read operations served over HTTP and MCP through the real ``invoke`` pipeline.

The reader is the installed ``NativeRunReader``. Its engine is a recorded producer
trace served per run, so root, child and grandchild histories are read exactly as the
native owner would answer them, with no model, store or network.
"""

from __future__ import annotations

import json
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from arbora.api.ops import runs as runs_module
from arbora.api.ops.run_graph import GRAPH_SCHEMA, step_key
from arbora.api.ops.runs import operations
from arbora.mcp_server import run_reader
from arbora.mcp_server.run_reader import NativeRunReader
from arbora.observability.state import SubsystemWindows
from arbora.run_origin import RunOriginRegistry
from tests.api.test_run_replay import event, reader_context, rows

ROOT = "work-item:" + "a" * 64
CHILD_TASK = "work-item:" + "b" * 64
GRAND_TASK = "work-item:" + "c" * 64
OWNER = {ROOT: "run-one", CHILD_TASK: "run-child", GRAND_TASK: "run-grand"}


def plan(run_id, digest, *tasks):
    now = int(time.time() * 1000)
    return {
        "run_id": run_id,
        "revision": 1,
        "observed_at_ms": now - 1000,
        "expires_at_ms": now + 60000,
        "items": [],
        "task_plan": {
            "plan_digest": digest,
            "total_tasks": len(tasks),
            "output_task_id": tasks[-1][0],
            "tasks": [
                {"task_id": name, "depends_on": deps, "prompt": "PRIVATE PROMPT"}
                for name, deps in tasks
            ],
        },
    }


def started(run_id, digest, sequence, task, deps=()):
    return event(
        sequence,
        run_id=run_id,
        type="task_started",
        status="running",
        task_id=task,
        parent_run_id=run_id,
        plan_id=digest,
        depends_on=list(deps),
    )


def spawn(run_id, sequence, task, child, child_task):
    return event(
        sequence,
        run_id=run_id,
        type="spawned_child",
        task_id=task,
        parent_run_id=run_id,
        child_run_id=child,
        child_task_id=child_task,
        thought="PRIVATE",
    )


def producer_trace():
    """Root fans out and joins, spawns a child; the child spawns a grandchild."""
    plans = {
        "run-one": plan(
            "run-one", "p-root", ("first", []), ("left", ["first"]), ("join", ["left"])
        ),
        "run-child": plan("run-child", "p-child", ("first", []), ("second", ["first"])),
        "run-grand": plan("run-grand", "p-grand", ("only", [])),
    }
    histories = {
        "run-one": [
            event(1),
            started("run-one", "p-root", 2, "first"),
            spawn("run-one", 3, "first", "run-child", CHILD_TASK),
        ],
        "run-child": [
            event(1, run_id="run-child"),
            started("run-child", "p-child", 2, "first"),
            spawn("run-child", 3, "first", "run-grand", GRAND_TASK),
        ],
        "run-grand": [
            event(1, run_id="run-grand"),
            started("run-grand", "p-grand", 2, "only"),
        ],
    }
    return plans, histories


def install(monkeypatch, plans, histories, *, owned=OWNER):
    calls = []

    def query(text, params):
        events = histories[params["run_id"]]
        calls.append(("page", params["run_id"]))
        if "AS event_id" in text:
            meta = [
                {
                    "sequence": e["sequence"],
                    "event_id": e["event_id"],
                    "event_type": e["type"],
                }
                for e in events
            ]
            return meta[: params["limit"]]
        return rows(*(e for e in events if e["sequence"] >= params["first"]))[
            : params["limit"]
        ]

    reader = NativeRunReader(
        lambda: SimpleNamespace(query_cypher=query), origins=RunOriginRegistry()
    )

    def authorized(task_id, scope):
        calls.append(("owner", task_id, scope))
        run = owned.get(task_id)
        if run is None:
            return None
        return {"status": "succeeded", "metadata": {"a2a_run_id": run}}

    async def native(context, run_id):
        calls.append(("plan", run_id))
        return plans.get(run_id)

    monkeypatch.setattr(reader._authority, "_authorized_item", authorized)
    monkeypatch.setattr(run_reader, "read_native_snapshot", native)
    return reader, calls


def graph_request(**changes):
    return {
        "run_id": "run-one",
        "task_id": ROOT,
        "schema_version": GRAPH_SCHEMA,
        **changes,
    }


async def serve(op_id, params, reader, *, allowed=True):
    """Return the HTTP response and the MCP result for one registered operation."""
    from arbora.api.http.app import create_api_application
    from arbora.api.http.auth import AmbientHTTPAuthenticator
    from arbora.api.invoke.pipeline import invoke
    from arbora.api.mcp.caller import caller_from_session
    from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
    from arbora.api.registry import Registry
    from tests.api.test_invoke_contract import setup_services

    op = next(item for item in operations() if item.id == op_id)
    handler = getattr(runs_module, "handle_" + op_id.replace(".", "_"))
    services, runtime, *_ = setup_services(op, registry=Registry([op]))
    context = reader_context()
    caller = context.caller
    if not allowed:
        caller = caller_from_session(
            replace(caller.session, scopes=frozenset()), credential_kind="bearer"
        )

    async def dispatch(operation, arguments, execution):
        bound = replace(
            context, caller=execution.caller, services={"run_trace_reader": reader}
        )
        return await handler(bound, arguments, operation)

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
        response = await client.post(f"/ops/{op_id}", json=params)
    projection = MCPProjection(
        services.registry,
        services,
        SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
        lambda: caller,
        services.policy_gate,
        invoke,
        windows=SubsystemWindows(),
    )
    return response, await dispatch_verb("ask", projection, op=op_id, params=params)


def keys(snapshot):
    return [json.loads(node["key"]) for node in snapshot["nodes"]]


@pytest.mark.spec("ARBORA-OPS-R021.4")
@pytest.mark.parametrize(
    "op_id,params",
    [
        ("runs.get", {"run_id": "run-one", "task_id": ROOT}),
        ("runs.events", {"run_id": "run-one", "task_id": ROOT}),
        ("runs.execution_graph", graph_request()),
    ],
)
async def test_registered_rest_mcp_parity(monkeypatch, op_id, params):
    plans, histories = producer_trace()
    reader, calls = install(monkeypatch, plans, histories)
    response, mcp = await serve(op_id, params, reader)
    assert response.status_code == 200, response.text
    assert mcp["ok"] and response.json()["result"] == mcp["result"]
    assert mcp["result"]["value"]["run_id"] == "run-one"
    assert "PRIVATE" not in json.dumps(mcp["result"])
    # A denied caller is refused identically on both transports with no read.
    calls.clear()
    denied, mcp_denied = await serve(op_id, params, reader, allowed=False)
    assert denied.status_code == 403 and not mcp_denied["ok"]
    assert calls == []


def child_key(run_id):
    return json.dumps([run_id, "child_run", run_id], separators=(",", ":"))


def check_nodes_and_edges(snapshot):
    assert snapshot["topology_completeness"] == "complete"
    assert snapshot["partial_reason"] is None
    assert snapshot["supports_spawned_children"] is True
    assert {tuple(k[:2]) for k in keys(snapshot)} == {
        ("run-one", "plan_step"),
        ("run-child", "child_run"),
        ("run-child", "plan_step"),
        ("run-grand", "child_run"),
        ("run-grand", "plan_step"),
    }
    spawned = [e for e in snapshot["edges"] if e["kind"] == "spawned_child"]
    assert [(e["source"], e["target"], e["evidence"]) for e in spawned] == [
        (
            step_key("run-one", "first"),
            child_key("run-child"),
            {"event_id": "event-3", "sequence": 3},
        ),
        (
            step_key("run-child", "first"),
            child_key("run-grand"),
            {"event_id": "event-3", "sequence": 3},
        ),
    ]
    # A dependency never implies parentage, and a spawn is never a dependency.
    depends = [e for e in snapshot["edges"] if e["kind"] == "depends_on"]
    assert len(depends) == 3 and len(snapshot["edges"]) == 5
    assert all("child_run" not in e["source"] + e["target"] for e in depends)


def check_depths_and_cursors(snapshot):
    depths = {
        node["run_id"]: node["depth"]
        for node in snapshot["nodes"]
        if node["kind"] == "child_run"
    }
    assert depths == {"run-child": 1, "run-grand": 2}
    assert [c["run_id"] for c in snapshot["run_cursors"]] == [
        "run-one",
        "run-child",
        "run-grand",
    ]
    for capability in ("can_read_transcript", "can_message_active"):
        assert snapshot[capability] is False


@pytest.mark.spec("ARBORA-OPS-R021.4.8.2")
async def test_root_child_grandchild_over_both_transports(monkeypatch):
    plans, histories = producer_trace()
    reader, calls = install(monkeypatch, plans, histories)
    response, mcp = await serve("runs.execution_graph", graph_request(), reader)
    assert response.status_code == 200, response.text
    assert response.json()["result"] == mcp["result"]
    snapshot = mcp["result"]["value"]["snapshot"]
    check_nodes_and_edges(snapshot)
    check_depths_and_cursors(snapshot)
    assert "PRIVATE" not in json.dumps(mcp["result"])
    # Restart and reconnect: a fresh reader over the same durable history agrees.
    again, _ = install(monkeypatch, plans, histories)
    reconnect, mcp_again = await serve("runs.execution_graph", graph_request(), again)
    assert mcp_again["result"] == mcp["result"]
    assert reconnect.json()["result"] == response.json()["result"]
    # Every child run was authorized before its plan or events were read.
    order = [c for c in calls if c[0] in ("owner", "plan")]
    for task, run in ((CHILD_TASK, "run-child"), (GRAND_TASK, "run-grand")):
        assert order.index(("owner", task, "kg:read")) < order.index(("plan", run))


@pytest.mark.spec("ARBORA-OPS-R021.4.8.2")
async def test_foreign_child_is_partial_and_never_read(monkeypatch):
    plans, histories = producer_trace()
    own = {ROOT: "run-one", CHILD_TASK: "run-child"}
    reader, calls = install(monkeypatch, plans, histories, owned=own)
    value = await reader.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    snapshot = value["snapshot"]
    assert snapshot["topology_completeness"] == "partial"
    assert snapshot["partial_reason"] == "child_unavailable"
    assert ("plan", "run-grand") not in calls
    assert ("page", "run-grand") not in calls
    # A foreign child answers exactly like an absent one.
    absent = {ROOT: "run-one", CHILD_TASK: "run-child"}
    other, _ = install(monkeypatch, plans, histories, owned=absent)
    assert (
        await other.execution_graph(
            context=reader_context(), request=_params(graph_request())
        )
        == value
    )


def _params(raw):
    from arbora.api.ops.run_graph import ExecutionGraphParams

    return ExecutionGraphParams.model_validate(raw)


@pytest.mark.spec("ARBORA-OPS-R021.4.8.2")
@pytest.mark.parametrize(
    "mutate",
    [
        "duplicate_child",
        "unknown_spawning_step",
        "wrong_parent",
        "self_child",
        "cycle",
    ],
)
async def test_conflicting_lineage_is_unavailable(monkeypatch, mutate):
    plans, histories = producer_trace()
    root = histories["run-one"]
    if mutate == "duplicate_child":
        root.append(spawn("run-one", 4, "first", "run-child", CHILD_TASK))
    elif mutate == "unknown_spawning_step":
        root[2] = spawn("run-one", 3, "ghost", "run-child", CHILD_TASK)
    elif mutate == "wrong_parent":
        root[2] = {**spawn("run-one", 3, "first", "run-child", CHILD_TASK)}
        root[2]["parent_run_id"] = "someone-else"
    elif mutate == "self_child":
        root[2] = spawn("run-one", 3, "first", "run-one", ROOT)
    else:
        histories["run-grand"].append(spawn("run-grand", 3, "only", "run-one", ROOT))
    reader, _ = install(monkeypatch, plans, histories)
    value = await reader.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    if mutate == "cycle":
        # The cycle sits two levels down: that child alone is unavailable.
        assert value["snapshot"]["partial_reason"] == "child_unavailable"
        assert ("run-one", "child_run", "run-one") not in [
            tuple(k) for k in keys(value["snapshot"])
        ]
    else:
        assert value["availability"] == "unavailable" and value["snapshot"] is None


@pytest.mark.spec("ARBORA-OPS-R021.4.8.2")
async def test_child_history_gap_and_cross_run_payload_are_typed_states(monkeypatch):
    plans, histories = producer_trace()
    histories["run-child"] = [
        event(1, run_id="run-child"),
        started("run-child", "p-child", 3, "first"),
    ]
    reader, _ = install(monkeypatch, plans, histories)
    value = await reader.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    assert value["snapshot"]["partial_reason"] == "child_unavailable"
    assert [k[:2] for k in keys(value["snapshot"])] == [["run-one", "plan_step"]] * 3
    plans, histories = producer_trace()
    histories["run-grand"][1] = started("run-other", "p-grand", 2, "only")
    reader, _ = install(monkeypatch, plans, histories)
    value = await reader.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    assert value["snapshot"]["partial_reason"] == "child_unavailable"
    assert "run-grand" not in json.dumps(value["snapshot"]["run_cursors"])


@pytest.mark.spec("ARBORA-OPS-R021.4.8.2")
@pytest.mark.parametrize("depth,reason", [(1, "depth_bound"), (2, None), (3, None)])
async def test_depth_bound_reports_partial(monkeypatch, depth, reason):
    from arbora.core.settings import runtime_config

    plans, histories = producer_trace()
    reader, _ = install(monkeypatch, plans, histories)
    monkeypatch.setattr(runtime_config, "runs_graph_max_depth", depth)
    value = await reader.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    snapshot = value["snapshot"]
    assert snapshot["partial_reason"] == reason
    followed = {k[0] for k in keys(snapshot) if k[1] == "child_run"}
    assert followed == set(["run-child", "run-grand"][:depth])


@pytest.mark.spec("ARBORA-OPS-R021.4.8.2")
@pytest.mark.parametrize(
    "bound,reason", [("nodes", "node_bound"), ("edges", "edge_bound")]
)
async def test_node_and_edge_bounds_drop_whole_children_and_say_partial(
    monkeypatch, bound, reason
):
    from arbora.core.settings import runtime_config

    plans, histories = producer_trace()
    reader, _ = install(monkeypatch, plans, histories)
    monkeypatch.setattr(runtime_config, f"runs_graph_max_{bound}", 10)
    plans["run-child"] = plan(
        "run-child",
        "p-child",
        *((f"s{n}", [f"s{n - 1}"] if n else []) for n in range(12)),
    )
    histories["run-child"] = [event(1, run_id="run-child")]
    value = await reader.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    snapshot = value["snapshot"]
    # The whole child is dropped, never half of it.
    assert snapshot["topology_completeness"] == "partial"
    assert snapshot["partial_reason"] in {reason, "child_unavailable"}
    assert all(k[0] == "run-one" for k in keys(snapshot))


@pytest.mark.spec("ARBORA-OPS-R021.4.8")
async def test_fan_out_fan_in_reconnect_and_foreign_owner(monkeypatch):
    plans, histories = producer_trace()
    # Repeated step names across runs keep run-qualified, distinct keys.
    reader, calls = install(monkeypatch, plans, histories)
    response, mcp = await serve("runs.execution_graph", graph_request(), reader)
    snapshot = mcp["result"]["value"]["snapshot"]
    names = [k for k in keys(snapshot) if k[1] == "plan_step"]
    assert [k[2] for k in names].count("first") == 2
    assert len({node["key"] for node in snapshot["nodes"]}) == len(snapshot["nodes"])
    # A bounded page: a long history is covered only to the bound.
    long = {**histories, "run-one": [event(n) for n in range(1, 1003)]}
    long["run-one"][2:3] = [started("run-one", "p-root", 3, "first")]
    bounded, _ = install(monkeypatch, plans, long)
    value = await bounded.execution_graph(
        context=reader_context(), request=_params(graph_request())
    )
    assert value["snapshot"]["covered_after"] == 1000
    assert value["snapshot"]["has_more"] is True
    # Reconnect over the same durable history gives the same bytes.
    again, _ = install(monkeypatch, plans, histories)
    assert (await serve("runs.execution_graph", graph_request(), again))[1][
        "result"
    ] == mcp["result"]
    # A foreign owner is refused before any payload read, over both transports.
    foreign, foreign_calls = install(monkeypatch, plans, histories, owned={})
    refused, refused_mcp = await serve("runs.execution_graph", graph_request(), foreign)
    assert refused.json()["result"]["value"]["availability"] == "unavailable"
    assert refused.json()["result"] == refused_mcp["result"]
    assert foreign_calls == [("owner", ROOT, "kg:read")] * 2
