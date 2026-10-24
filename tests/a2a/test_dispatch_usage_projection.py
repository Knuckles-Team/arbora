"""Dispatch projection keeps native receipt and legacy snapshot paths distinct."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.mcp_server.task_plan import TaskPlanConfig

pytestmark = pytest.mark.spec("ARBORA-A2A-R001")


def projection(monkeypatch, *, native):
    config = TaskPlanConfig({}, {}, 1, 2) if native else None
    authority = WorkItemA2AAuthority(lambda: None, task_plan_config=config)
    task = SimpleNamespace(id="task", metadata={"arbora": {"runId": "run"}})
    monkeypatch.setattr(authority, "_project", Mock(return_value=task))
    attach = AsyncMock()
    monkeypatch.setattr(authority, "_attach_run_usage", attach)
    return authority, task, attach


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {}, {"requests": 1, "total_tokens": 5}])
async def test_legacy_snapshot_keeps_absence_and_empty_distinct(monkeypatch, usage):
    authority, task, attach = projection(monkeypatch, native=False)
    item, session = {}, object()
    result = await authority._project_dispatch_task(
        item, task_id="task", context_id="context", session=session, usage=usage
    )
    assert result is task
    authority._project.assert_called_once_with(item, task_id="task")
    attach.assert_not_awaited()
    if usage is None:
        assert "usage" not in task.metadata["arbora"]
    else:
        assert task.metadata["arbora"]["usage"] == {
            "runId": "run",
            "taskId": "task",
            "contextId": "context",
            "scope": "turn",
            "counts": usage,
        }
        assert task.metadata["arbora"]["usage"]["counts"] is not usage


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [None, {}, {"total_tokens": 999}])
async def test_native_path_never_substitutes_immediate_counts(monkeypatch, usage):
    authority, task, attach = projection(monkeypatch, native=True)
    item, session = {}, object()
    result = await authority._project_dispatch_task(
        item, task_id="task", context_id="context", session=session, usage=usage
    )
    assert result is task
    attach.assert_awaited_once_with(task, item, session)
    assert "usage" not in task.metadata["arbora"]


@pytest.mark.asyncio
async def test_native_refusal_propagates_without_legacy_fallback(monkeypatch):
    authority, task, attach = projection(monkeypatch, native=True)
    attach.side_effect = PermissionError("caller changed")
    with pytest.raises(PermissionError, match="caller changed"):
        await authority._project_dispatch_task(
            {},
            task_id="task",
            context_id="context",
            session=object(),
            usage={"total_tokens": 999},
        )
    assert "usage" not in task.metadata["arbora"]
