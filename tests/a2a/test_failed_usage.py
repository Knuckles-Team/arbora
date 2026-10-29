"""Controlled A2A commit transport with the real AU executor and planner."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from agent_utilities import host_ports
from agent_utilities.graph import RunFailed, RunResult

from arbora import agent_runs, work_items
from arbora.a2a.authority import WorkItemA2AAuthority, task_output_artifacts
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from tests.mcp_server.test_task_plan_failure import USAGE, fail_at, scope
from tests.mcp_server.test_task_plan_runtime import bridge as bridge
from tests.mcp_server.test_task_plan_runtime import chain as chain
from tests.test_native_run_scope import CLAIM, SESSION

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.2.1.2")


async def invoke(monkeypatch, execute, *, commit_error=False):
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: CLAIM)
    commits = []

    def commit(*args, **kwargs):
        commits.append(kwargs)
        if commit_error:
            raise RuntimeError("native commit failed")
        return "committed"

    monkeypatch.setattr(work_items, "commit_result", commit)
    authority = WorkItemA2AAuthority(lambda: None)
    monkeypatch.setattr(authority, "_execute_claimed_run", execute)
    usage = await authority._run(
        object(),
        item_id="work",
        session=SESSION,
        agent_name="helper",
        task="question",
        task_id="failed-task",
        run_id="run",
    )
    assert commits == [
        {"outcome": "failed", "result_ref": None, "error_ref": commits[0]["error_ref"]}
    ]
    assert task_output_artifacts("failed-task") is None
    return usage


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase",
    ["json", "receipt", "publication", "readback", "reauthorization", "provider"],
)
async def test_real_executor_failure_receipt(chain, monkeypatch, phase):
    host, _, calls = chain
    if phase == "provider":
        from types import SimpleNamespace

        from arbora.mcp_server import task_plan_runtime

        monkeypatch.setattr(
            task_plan_runtime,
            "Agent",
            lambda *a, **k: SimpleNamespace(
                run=AsyncMock(side_effect=RuntimeError("provider"))
            ),
        )
    else:
        fail_at(phase, monkeypatch)
    events = []
    host_ports.register_run_event_sink(events.append)

    async def execute(*args, **kwargs):
        with scope(host):
            return await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(
                    agent_name="helper", task="question", run_id="run"
                ),
                session=SESSION,
            )

    try:
        usage = await invoke(monkeypatch, execute)
    finally:
        host_ports.register_run_event_sink(None)
    assert usage == events[-1]["result"]["usage"]
    assert usage == ({} if phase == "provider" else USAGE)
    assert len(events) == 1
    assert len(calls) == (0 if phase == "provider" else 1)


def failure(run_id="run", usage=None):
    return RunFailed(
        RunResult(
            run_id=run_id,
            status="failed",
            output="private partial",
            error="failure",
            steps=0,
            usage=USAGE if usage is None else usage,
        )
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["plain", "forged", "foreign", "malformed", "overflow", "subclass"]
)
async def test_untrusted_failures_commit_without_usage(monkeypatch, kind):
    error = RuntimeError("failure")
    if kind == "forged":
        error.result = failure().result
        error.usage = USAGE
    elif kind == "foreign":
        error = failure("other")
    elif kind == "malformed":
        error = failure()
        error._result = object()
    elif kind == "overflow":
        error = failure(usage={**USAGE, "requests": 2**53})
    elif kind == "subclass":

        class Derived(RunFailed):
            pass

        error = Derived(failure().result)
    assert await invoke(monkeypatch, AsyncMock(side_effect=error)) is None


@pytest.mark.asyncio
async def test_failed_commit_does_not_return_receipt(monkeypatch):
    with pytest.raises(RuntimeError, match="native commit failed"):
        await invoke(monkeypatch, AsyncMock(side_effect=failure()), commit_error=True)


@pytest.mark.asyncio
async def test_cancel_does_not_commit(monkeypatch):
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: CLAIM)
    monkeypatch.setattr(
        work_items, "commit_result", lambda *a, **k: pytest.fail("commit")
    )
    authority = WorkItemA2AAuthority(lambda: None)
    monkeypatch.setattr(
        authority, "_execute_claimed_run", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await authority._run(
            object(),
            item_id="work",
            session=SESSION,
            agent_name="helper",
            task="question",
        )


def test_readonly_counters_are_copied():
    error = failure()
    projected = agent_runs.failed_run_usage(error, "run")
    projected["requests"] = 99
    assert error.usage == USAGE


def test_missing_public_failure_contract(monkeypatch):
    import agent_utilities.graph as au

    from arbora.mcp_server import task_plan_runtime

    monkeypatch.delattr(au, "RunFailed")
    with pytest.raises(task_plan_runtime.OperationRefused, match="UPGRADE_REQUIRED"):
        task_plan_runtime._contracts()
