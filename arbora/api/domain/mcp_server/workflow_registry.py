"""The planner's compiled-workflow lookup, resolved through the engine registry (ARBORA-HOST-R022.1).

The engine ``WorkflowRegistry`` owns registration and read (MEDULLA-DECISION-ENGINE-R166.7);
arbora holds no workflow store. A task names a registered workflow as
``workflow:<workflow_id>@<version>#<content_digest>`` and the lookup answers that exact
registration through ``WorkflowRegistry`` read, with the skills it declares. The engine
client is built on the first lookup, once, under a lock, so serving start builds none and
concurrent first lookups share one. A workflow the engine does not know, or one the caller
may not read, refuses ``NOT_FOUND`` with nothing beyond the engine's own answer.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused

#: A task that names a registered workflow.
WORKFLOW_IRI = re.compile(
    r"^workflow:(?P<workflow_id>[^@#\s]+)@(?P<version>[^@#\s]+)#(?P<content_digest>\S+)$"
)
#: The engine refusals that read as an absent workflow.
ABSENT = ("WORKFLOW_IDENTITY_UNKNOWN", "WORKFLOW_UNAUTHORIZED")


class LazyEngineClient:
    """The engine client, created on first use and shared after (guarded, one start)."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._lock = threading.Lock()
        self._client: Any = None
        self.starts = 0

    def get(self) -> Any:
        with self._lock:
            if self._client is None:
                self._client = self._factory()
                self.starts += 1
            return self._client


def _refused_as_absent(error: Exception) -> bool:
    text = f"{getattr(error, 'code', '')} {error}"
    return any(code in text for code in ABSENT)


def _hit(entry: Any, identity: dict[str, str]) -> dict[str, Any]:
    body = entry if isinstance(entry, dict) else entry.model_dump(mode="json")
    return {
        "kind": "workflow",
        "id": identity["workflow_id"],
        "version": identity["version"],
        "content_digest": identity["content_digest"],
        "lifecycle": str(body.get("lifecycle", "")),
        "skills": list(body.get("skills") or ()),
    }


class WorkflowRegistryLookup:
    """``Sequence[str] -> hits``: each task that names a workflow, read from the engine."""

    def __init__(self, client: LazyEngineClient, graph: str) -> None:
        self._client = client
        self._graph = graph

    async def __call__(self, task_iris: Sequence[str]) -> list[dict[str, Any]]:
        hits: list[dict[str, Any]] = []
        for iri in task_iris:
            matched = WORKFLOW_IRI.match(iri)
            if matched is not None:
                hits.append(await self._read(matched.groupdict()))
        return hits

    async def _read(self, identity: dict[str, str]) -> dict[str, Any]:
        from medulla.generated import coordination

        client = self._client.get()
        try:
            answer = await coordination.send_workflow_registry(
                client, {"op": "read", "identity": identity}, self._graph
            )
            entry = coordination.decode_workflow_registry_read(answer)
        except Exception as error:
            if _refused_as_absent(error):
                raise OperationRefused("NOT_FOUND") from None
            raise
        return _hit(entry, identity)


def workflow_lookup(
    client_factory: Callable[[], Any], graph: str
) -> WorkflowRegistryLookup:
    """The lookup over a client that is not built until the first read."""
    if not graph.strip():
        raise LookupError("the verified session is not bound to a graph")
    return WorkflowRegistryLookup(LazyEngineClient(client_factory), graph)


__all__ = [
    "LazyEngineClient",
    "WorkflowRegistryLookup",
    "workflow_lookup",
]
