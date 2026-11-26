"""One registered workflow definition for the workflow-host tests."""

from __future__ import annotations

from arbora.control_plane.workflows import (
    InMemoryWorkflowRepository,
    WorkflowDefinition,
)
from tests.control_plane.test_workflows import _definition

WORKFLOW_ID = "workflow:research"


def register(repository: InMemoryWorkflowRepository) -> WorkflowDefinition:
    definition = _definition()
    repository.put_version(definition)
    return definition
