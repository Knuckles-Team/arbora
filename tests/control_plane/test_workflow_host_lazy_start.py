"""The workflow engine starts once, on first use (ARBORA-OPS-R873)."""

from __future__ import annotations

import pytest

from arbora.api.errors import EngineRefusal
from arbora.control_plane.workflows import InMemoryWorkflowRepository
from arbora.control_plane.workflows.host import WorkflowHost
from tests.control_plane.workflow_fixtures import WORKFLOW_ID, register


@pytest.mark.spec("ARBORA-OPS-R873")
def test_start_counter_zero_then_one() -> None:
    repository = InMemoryWorkflowRepository()
    register(repository)
    built: list[object] = []

    def factory() -> object:
        built.append(object())
        return built[-1]

    host = WorkflowHost(repository, factory)
    assert host.starts == 0 and built == []
    first, receipt = host.invoke(WORKFLOW_ID, "1.0.0")
    second, _ = host.invoke(WORKFLOW_ID, "1.0.0")
    assert first is second and host.starts == 1 and len(built) == 1
    assert receipt["engine_starts"] == 1
    assert receipt["workflow_identity"]["workflow_id"] == WORKFLOW_ID

    cold = WorkflowHost(repository, factory)
    with pytest.raises(EngineRefusal) as unknown:
        cold.invoke("wf-missing")
    assert unknown.value.code == "WORKFLOW_IDENTITY_UNKNOWN"
    assert cold.starts == 0 and len(built) == 1
