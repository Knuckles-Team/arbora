"""Do not publish a completed reply before its terminal commit succeeds."""

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arbora import work_items
from arbora.a2a import authority as module
from arbora.api.contracts.agent_control_contracts import AgentExecutionResult

pytestmark = pytest.mark.spec("ARBORA-A2A-R001.3")
TASK = "a2a-" + "a" * 64
CONTEXT = "a2a-context-" + "b" * 64


@pytest.fixture
def execution(monkeypatch):
    # Keep this regression independent of other task-output cache users.
    monkeypatch.setattr(module, "_OUTPUTS", OrderedDict())
    owner = module.WorkItemA2AAuthority(lambda: None)
    execute = AsyncMock(
        return_value=AgentExecutionResult(run_id="run", output="completed answer")
    )
    monkeypatch.setattr(owner, "_execute_claimed_run", execute)
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: {"claimed": True})
    return owner, execute


async def run(owner):
    return await owner._run(
        object(),
        item_id="work",
        session=SimpleNamespace(tenant="tenant"),
        agent_name="helper",
        task="question",
        task_id=TASK,
        run_id="run",
    )


@pytest.mark.asyncio
async def test_reply_is_not_visible_inside_terminal_commit(execution, monkeypatch):
    owner, execute = execution
    observations = []

    def commit(*args, **kwargs):
        observations.append(module.task_output_artifacts(TASK))
        return "committed"

    monkeypatch.setattr(work_items, "commit_result", commit)
    await run(owner)
    execute.assert_awaited_once()
    assert len(observations) == 1
    assert observations == [None], "reply was published before terminal commit"
    assert module.task_output_artifacts(TASK) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
async def test_commit_failure_leaves_no_reply_and_does_not_replay(
    execution, monkeypatch, failure
):
    owner, execute = execution
    commits = []

    def commit(*args, **kwargs):
        commits.append(kwargs)
        raise failure("terminal commit refused or outcome uncertain")

    monkeypatch.setattr(work_items, "commit_result", commit)
    with pytest.raises(failure, match="terminal commit"):
        await run(owner)
    # Verify no automatic execution or commit replay before the red cache check.
    execute.assert_awaited_once()
    assert len(commits) == 1
    assert not owner._detached
    projected = owner._project(
        {
            "status": "running",
            "metadata": {"a2a_context_id": CONTEXT},
            "work_item_id": "work",
        },
        task_id=TASK,
    )
    assert projected.status.state == "working"
    assert projected.artifacts is None
    assert module.task_output_artifacts(TASK) is None, (
        "reply remained cached after terminal commit failed"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "noop", "running", "failed", True])
async def test_unconfirmed_commit_does_not_publish_or_replay(
    execution, monkeypatch, status
):
    owner, execute = execution
    monkeypatch.setattr(work_items, "commit_result", lambda *a, **k: status)
    with pytest.raises(RuntimeError, match="not committed"):
        await run(owner)
    execute.assert_awaited_once()
    assert module.task_output_artifacts(TASK) is None


@pytest.mark.asyncio
async def test_cancelled_commit_has_no_late_cache_publication(execution, monkeypatch):
    import asyncio
    import threading

    owner, execute = execution
    entered = asyncio.Event()
    release = threading.Event()
    finished = threading.Event()
    loop = asyncio.get_running_loop()
    commits = []

    def commit(*args, **kwargs):
        commits.append(kwargs)
        loop.call_soon_threadsafe(entered.set)
        try:
            if not release.wait(timeout=5):
                raise TimeoutError("test did not release commit")
            return "committed"
        finally:
            finished.set()

    monkeypatch.setattr(work_items, "commit_result", commit)
    running = asyncio.create_task(run(owner))
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
    finally:
        release.set()
        assert await asyncio.to_thread(finished.wait, 5)
    execute.assert_awaited_once()
    assert len(commits) == 1
    assert module.task_output_artifacts(TASK) is None


@pytest.mark.asyncio
async def test_new_claim_clears_stale_reply_before_failed_commit(
    execution, monkeypatch
):
    owner, execute = execution
    module.remember_task_output(TASK, "stale reply")

    def commit(*args, **kwargs):
        assert module.task_output_artifacts(TASK) is None
        raise RuntimeError("terminal commit refused")

    monkeypatch.setattr(work_items, "commit_result", commit)
    with pytest.raises(RuntimeError, match="terminal commit"):
        await run(owner)
    execute.assert_awaited_once()
    assert module.task_output_artifacts(TASK) is None
