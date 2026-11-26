"""The workflow host: a lazily started engine and the registry identity of each run.

Registration builds no engine. ``WorkflowHost.engine`` starts it once, on the first
invocation, under a lock, and reuses it (ARBORA-OPS-R873). An unknown workflow refuses
before any start with the engine code ``WORKFLOW_IDENTITY_UNKNOWN``, and a known one
resolves to the engine registry identity of its version (ARBORA-OPS-R874).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from arbora.api.errors import EngineRefusal

from .domain import WorkflowRegistryIdentity
from .repository import WorkflowRepository

UNKNOWN = "WORKFLOW_IDENTITY_UNKNOWN"


class WorkflowHost:
    """Holds the repository and the lazily built engine; keeps no run state."""

    def __init__(
        self, repository: WorkflowRepository, engine_factory: Callable[[], Any]
    ) -> None:
        self._repository = repository
        self._factory = engine_factory
        self._lock = threading.Lock()
        self._engine: Any = None
        self.starts = 0

    def resolve(
        self, workflow_id: str, version: str | None = None, channel: str = "stable"
    ) -> WorkflowRegistryIdentity:
        """The registry identity of the workflow, or ``WORKFLOW_IDENTITY_UNKNOWN``."""
        identity = self._repository.match(workflow_id, version, channel)
        if identity is None:
            raise EngineRefusal(UNKNOWN)
        return identity

    def engine(self) -> Any:
        """The engine, started on first use and reused after."""
        with self._lock:
            if self._engine is None:
                self._engine = self._factory()
                self.starts += 1
            return self._engine

    def invoke(
        self, workflow_id: str, version: str | None = None, channel: str = "stable"
    ) -> tuple[Any, dict[str, Any]]:
        """Resolve first, then start: an unknown id never starts the engine.

        The receipt records the resolved identity and the one start event.
        """
        identity = self.resolve(workflow_id, version, channel)
        engine = self.engine()
        return engine, {
            "workflow_identity": identity.model_dump(mode="json"),
            "engine_starts": self.starts,
        }


__all__ = ["UNKNOWN", "WorkflowHost"]
