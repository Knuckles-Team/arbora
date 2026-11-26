"""The host match returns the engine registry identity (ARBORA-OPS-R874)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.ops import agents
from arbora.control_plane.workflows import (
    InMemoryWorkflowRepository,
    WorkflowRegistryIdentity,
)
from arbora.control_plane.workflows.host import WorkflowHost
from tests.api._engine_double import context as engine_context
from tests.control_plane.workflow_fixtures import WORKFLOW_ID, register


@pytest.mark.spec("ARBORA-OPS-R874")
async def test_entry_resolves_registered_record_and_unknown_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = InMemoryWorkflowRepository()
    definition = register(repository)
    identity = repository.match(WORKFLOW_ID, "1.0.0")
    assert identity == WorkflowRegistryIdentity(
        workflow_id=WORKFLOW_ID,
        version="1.0.0",
        content_digest=definition.definition_digest,
    )
    assert repository.match(WORKFLOW_ID, "9.9.9") is None

    sent: list[tuple[str, dict[str, Any]]] = []

    async def send(context: Any, method: str, params: dict[str, Any], **_: Any) -> Any:
        sent.append((method, params))
        return {}

    monkeypatch.setattr(agents, "engine_send", send)
    host = WorkflowHost(repository, object)
    context = engine_context()
    context.services = {"workflow_host": host}
    message = {"metadata": {"workflow_id": WORKFLOW_ID, "workflow_version": "1.0.0"}}
    admitted = await agents.admit_workflow(context, message)
    wire = identity.model_dump(mode="json")
    assert admitted["metadata"]["workflow_identity"] == wire
    assert sent == [("WorkflowRegistry", {"op": "read", "identity": wire})]
    assert host.starts == 1

    unknown = {"metadata": {"workflow_id": "workflow:absent"}}
    with pytest.raises(EngineRefusal) as refused:
        await agents.admit_workflow(context, unknown)
    assert refused.value.code == "WORKFLOW_IDENTITY_UNKNOWN"
    assert len(sent) == 1 and host.starts == 1
    assert await agents.admit_workflow(context, {"metadata": {}}) == {"metadata": {}}
