"""Native receipt consumer boundaries, not native persistence qualification."""

import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from epistemic_graph.generated.models import RunWorkItemView

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.run_usage import project_run_usage
from arbora.api.contracts.agent_control_contracts import AgentExecutionResult
from tests.test_native_run_admission import request
from tests.test_native_run_scope import CLAIM, SESSION

pytestmark = pytest.mark.spec("ARBORA-A2A-R001")
COUNTS = {
    "requests": 1,
    "tool_calls": 0,
    "input_tokens": 3,
    "output_tokens": 2,
    "total_tokens": 5,
}


def record(outcome="succeeded", counts=COUNTS):
    admission = request()
    admission["kind"] = "orchestrator_task"
    admission["metadata"] = {
        "a2a_schema": "arbora-a2a-unary-v1",
        "a2a_owner_ref": "owner",
        "a2a_context_id": "a2a-context-" + "b" * 64,
        "a2a_run_id": "run",
    }
    return {
        "admission": admission,
        "work_item": {
            "work_item_id": ("work-item:" + "a" * 64),
            "kind": "orchestrator_task",
            "status": outcome,
            "input_ref": "input",
            "version": 2,
            "updated_at_ms": 300,
            "metadata": deepcopy(admission["metadata"]),
        },
        "run_completion": {
            "payload": {
                "completion": {"schema_version": 1, "run_id": "run", "usage": counts},
                "tenant": "tenant",
                "work_item_id": ("work-item:" + "a" * 64),
                "batch_id": "batch",
                "outcome": outcome,
                "result_ref": None,
                "error_ref": None,
            },
            "scope": {
                "tenant": "physical-owner",
                "scope": {"kind": "graph", "graph": "physical-graph"},
                "incarnation_id": "generation",
                "identity_digest": [1] * 32,
            },
            "commit_sequence": 1,
            "ordinal": 0,
            "payload_digest": "a" * 64,
        },
    }


def project(value, item=None):
    item = item or value["work_item"]
    task = WorkItemA2AAuthority._project(item, task_id=("work-item:" + "a" * 64))
    return project_run_usage(
        value,
        session=SimpleNamespace(tenant="tenant"),
        task=task,
        item=item,
        owner_ref="owner",
        owner_graph="private-owner",
    )


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
@pytest.mark.parametrize("counts", [COUNTS, {}])
def test_restart_projection_keeps_exact_snapshot(outcome, counts):
    value = record(outcome, counts)
    assert project(value)["counts"] == counts
    if outcome == "failed":
        assert (
            WorkItemA2AAuthority._project(
                value["work_item"], task_id=("work-item:" + "a" * 64)
            ).artifacts
            is None
        )


def test_absent_legacy_receipt_is_unavailable():
    value = record()
    del value["run_completion"]
    assert project(value)["status"] == "unavailable"


@pytest.mark.parametrize(
    "path",
    [
        ("admission", "metadata", "a2a_owner_ref"),
        ("admission", "metadata", "a2a_context_id"),
        ("admission", "context", "graph"),
        ("run_completion", "payload", "completion", "run_id"),
        ("run_completion", "payload", "tenant"),
        ("run_completion", "payload", "work_item_id"),
    ],
)
def test_foreign_receipt_refuses(path):
    value = record()
    item = deepcopy(value["work_item"])
    target = value
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = "foreign"
    # Prove the mutant is schema-valid before testing consumer binding.
    RunWorkItemView.model_validate_json(json.dumps(value), strict=True)
    error = PermissionError if path[0] == "admission" else ValueError
    message = "admission scope" if path[0] == "admission" else "terminal task"
    with pytest.raises(error, match=message):
        project(value, item)


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
@pytest.mark.parametrize("counts", [COUNTS, {}])
async def test_snapshot_is_committed_once(monkeypatch, failed, counts):
    from arbora import work_items
    from arbora.a2a.authority import remember_task_output, task_output_artifacts
    from tests.a2a.test_failed_usage import failure

    authority = WorkItemA2AAuthority(lambda: None, task_plan_config=object())
    result = AgentExecutionResult(run_id="run", output="answer", usage=counts)
    execute = AsyncMock(return_value=result)
    if failed:
        remember_task_output("task", "stale answer")
        execute.side_effect = failure(usage=counts)
    monkeypatch.setattr(authority, "_execute_claimed_run", execute)
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: CLAIM)
    commits = []
    monkeypatch.setattr(work_items, "commit_result", lambda *a, **k: commits.append(k))
    await authority._run(
        object(),
        item_id="work",
        session=SESSION,
        agent_name="helper",
        task="question",
        task_id="task",
        run_id="run",
    )
    assert len(commits) == 1
    assert commits[0]["run_completion"] == {
        "schema_version": 1,
        "run_id": "run",
        "usage": counts,
    }
    assert commits[0]["outcome"] == ("failed" if failed else "succeeded")
    if failed:
        assert commits[0]["result_ref"] is None
        assert task_output_artifacts("task") is None


def test_native_commit_port_sends_snapshot_without_addition(monkeypatch):
    from arbora import work_items

    sent = []
    namespace = SimpleNamespace(
        commit_result=lambda **kwargs: sent.append(kwargs) or {"status": "failed"}
    )
    monkeypatch.setattr(work_items, "_session", lambda: SESSION)
    completion = {"schema_version": 1, "run_id": "run", "usage": COUNTS}
    assert (
        work_items.commit_result(
            SimpleNamespace(work_items=namespace),
            "work",
            CLAIM,
            outcome="failed",
            run_completion=completion,
        )
        == "committed"
    )
    assert len(sent) == 1
    assert sent[0]["run_completion"] == completion
    assert sent[0]["result_ref"] is None
    assert sent[0]["fencing_token"] == CLAIM["fencing_token"]


@pytest.mark.asyncio
async def test_get_run_uses_owner_graph_and_caller_tenant():
    from arbora.a2a.run_usage import read_run_usage

    lookup = AsyncMock(return_value=None)
    graphs = []
    client = SimpleNamespace(work_items=SimpleNamespace(get_run=lookup))
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: (
                graphs.append(graph) or SimpleNamespace(async_client=client)
            )
        )
    )
    value = record()
    item = value["work_item"]
    task = WorkItemA2AAuthority._project(item, task_id=item["work_item_id"])
    result = await read_run_usage(
        engine,
        session=SimpleNamespace(tenant="tenant"),
        task=task,
        item=item,
        owner_ref="owner",
        owner_graph="private-owner",
    )
    assert graphs == ["private-owner"]
    lookup.assert_awaited_once_with(tenant="tenant", run_id="run")
    assert result["status"] == "unavailable"


@pytest.mark.parametrize("outcome", ["succeeded", "failed"])
@pytest.mark.parametrize("counts", [COUNTS, {}])
def test_full_run_view_native_json_roundtrip(outcome, counts):
    value = record(outcome, counts)
    octets = [0, 127, 128, 255] * 8
    value["run_completion"]["scope"]["identity_digest"] = octets
    view = RunWorkItemView.model_validate_json(json.dumps(value), strict=True)
    assert view.run_completion.scope.identity_digest == bytes(octets)
    encoded = view.model_dump_json(exclude_none=True)
    decoded = json.loads(encoded)
    assert decoded["run_completion"]["scope"]["identity_digest"] == octets
    assert RunWorkItemView.model_validate_json(encoded, strict=True) == view
    assert project(decoded)["counts"] == counts


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", [COUNTS, {}])
async def test_get_restores_failed_usage_from_owner_bound_receipt(monkeypatch, counts):
    from arbora import work_items

    value = record("failed", counts)
    item = value["work_item"]
    lookup = AsyncMock(return_value=value)
    graphs = []
    client = SimpleNamespace(work_items=SimpleNamespace(get_run=lookup))
    engine = SimpleNamespace(
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: (
                graphs.append(graph) or SimpleNamespace(async_client=client)
            )
        )
    )
    session = SimpleNamespace(tenant="tenant")
    authority = WorkItemA2AAuthority(lambda: engine)
    monkeypatch.setattr(authority, "_session", lambda scope: session)
    monkeypatch.setattr(authority, "_owner_ref", lambda session: "owner")
    monkeypatch.setattr(authority, "_engine", lambda: (engine, object()))
    monkeypatch.setattr(authority, "_authorized_item", lambda *args: item)
    monkeypatch.setattr(
        work_items, "native_owner_graph", lambda engine: "private-owner"
    )
    task = await authority.get(item["work_item_id"])
    assert task.metadata["arbora"]["usage"]["counts"] == counts
    assert task.artifacts is None
    assert graphs == ["private-owner"]
    lookup.assert_awaited_once_with(tenant="tenant", run_id="run")


@pytest.mark.asyncio
async def test_cancelled_native_run_is_not_wrapped_or_committed(monkeypatch):
    from arbora import work_items

    authority = WorkItemA2AAuthority(lambda: None, task_plan_config=object())
    monkeypatch.setattr(
        authority, "_execute_claimed_run", AsyncMock(side_effect=asyncio.CancelledError)
    )
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: CLAIM)
    monkeypatch.setattr(
        work_items, "commit_result", lambda *a, **k: pytest.fail("commit after cancel")
    )
    with pytest.raises(asyncio.CancelledError):
        await authority._run(
            object(),
            item_id="work",
            session=SESSION,
            agent_name="helper",
            task="question",
            task_id="task",
            run_id="run",
        )
