"""Actual AU terminal accounting with a controlled model and EG transport."""

import asyncio
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from agent_utilities import host_ports
from agent_utilities.graph import RunPlanFailure, RunRequest, run

from arbora import agent_runs
from arbora.mcp_server import task_plan_runtime
from tests.mcp_server.test_task_plan_runtime import bridge as bridge
from tests.mcp_server.test_task_plan_runtime import chain as chain
from tests.test_native_run_scope import CLAIM, SESSION

pytestmark = pytest.mark.spec("ARBORA-HOST-R001.2.1.2")
USAGE = dict(requests=1, tool_calls=0, input_tokens=7, output_tokens=3, total_tokens=10)


def scope(host):
    return agent_runs.native_run_scope(
        run_id="run",
        task_id="task",
        context_id="context",
        work_item_id="work",
        session=SESSION,
        claim=CLAIM,
        owner_graph="owner-graph",
        hosted_invocation=host,
    )


def fail_at(phase, monkeypatch):
    _, eg = task_plan_runtime._contracts()
    if phase == "json":
        monkeypatch.setattr(
            task_plan_runtime,
            "_decode_proposal",
            lambda _: __import__("json").loads("{"),
        )
    elif phase == "receipt":
        eg.decode_publish_current_plan = lambda _: SimpleNamespace(run_id="wrong")
    elif phase == "publication":
        eg.send_publish_current_plan = AsyncMock(
            side_effect=RuntimeError("publish failed")
        )
    elif phase == "readback":
        eg.decode_get_current_plan = lambda _: None
    elif phase == "reauthorization":
        admit = task_plan_runtime._admit
        calls = []

        async def reauthorize(host, caller, operation):
            calls.append(operation)
            if calls.count("agents.run") == 2:
                raise PermissionError("revoked")
            return await admit(host, caller, operation)

        monkeypatch.setattr(task_plan_runtime, "_admit", reauthorize)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", ["json", "receipt", "publication", "readback", "reauthorization"]
)
async def test_failed_plan_terminal_ledger_exactly_once(chain, monkeypatch, phase):
    host, _, calls = chain
    fail_at(phase, monkeypatch)
    events = []
    host_ports.register_run_event_sink(events.append)
    try:
        with scope(host):
            result = await run(RunRequest(task="question", run_id="run"))
    finally:
        host_ports.register_run_event_sink(None)
    assert result.status == "failed"
    assert result.usage == USAGE
    assert len(events) == 1
    assert events[0]["result"] == asdict(result)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["cancel", "timeout"])
async def test_interrupted_plan_preserves_measured_usage(chain, kind):
    host, _, calls = chain
    _, eg = task_plan_runtime._contracts()
    reached = asyncio.Event()

    async def pause(*args):
        reached.set()
        await asyncio.Future()

    eg.send_publish_current_plan = pause
    if kind == "timeout":
        host = replace(
            host, task_plan_config=replace(host.task_plan_config, timeout_seconds=0.05)
        )
    events = []
    host_ports.register_run_event_sink(events.append)

    async def execute():
        with scope(host):
            return await run(RunRequest(task="question", run_id="run"))

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(reached.wait(), 5)
        if kind == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await task
            assert result.status == "failed" and "TimeoutError" in result.error
            assert result.usage == USAGE
        assert len(events) == 1
        assert events[0]["result"]["usage"] == USAGE
        assert events[0]["type"] == ("run_cancelled" if kind == "cancel" else "error")
        assert len(calls) == 1
    finally:
        host_ports.register_run_event_sink(None)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_provider_failure_does_not_borrow_stale_owner_ledger(chain, monkeypatch):
    host, _, calls = chain
    error = RuntimeError("provider failed before reporting usage")
    monkeypatch.setattr(
        task_plan_runtime,
        "Agent",
        lambda *a, **k: SimpleNamespace(run=AsyncMock(side_effect=error)),
    )
    with scope(host):
        request = RunRequest(task="question", run_id="run")
        agent_runs.current_native_run(request).planning_usage.update(USAGE)
        with pytest.raises(RuntimeError) as caught:
            await agent_runs.decide_run(request)
    assert caught.value is error
    assert not isinstance(caught.value, RunPlanFailure)
    assert not calls


def test_missing_failure_contract_refuses_before_planning(monkeypatch):
    import agent_utilities.graph as au

    monkeypatch.delattr(au, "RunPlanFailure")
    with pytest.raises(task_plan_runtime.OperationRefused, match="UPGRADE_REQUIRED"):
        task_plan_runtime._contracts()


@pytest.mark.asyncio
async def test_unreported_provider_failure_has_no_fabricated_usage(chain, monkeypatch):
    host, _, calls = chain
    monkeypatch.setattr(
        task_plan_runtime,
        "Agent",
        lambda *a, **k: SimpleNamespace(
            run=AsyncMock(side_effect=RuntimeError("provider"))
        ),
    )
    events = []
    host_ports.register_run_event_sink(events.append)
    try:
        with scope(host):
            result = await run(RunRequest(task="question", run_id="run"))
    finally:
        host_ports.register_run_event_sink(None)
    assert result.status == "failed"
    assert all(value == 0 for value in result.usage.values())
    assert len(events) == 1
    assert events[0]["result"]["usage"] == result.usage
    assert not calls
