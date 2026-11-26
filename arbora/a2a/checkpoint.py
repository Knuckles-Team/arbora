"""The host checkpoint port of a native run, over the engine's checkpoint methods.

Arbora holds no checkpoint. ``EngineHostCheckpoint`` writes each checkpoint as
one engine snapshot (``PublishRunCheckpoint``) under the live claim fence and the
expected checkpoint revision, and reads it back (``GetRunCheckpoint``) rehashed.
A checkpoint of another tenant or run reads as absent. The harness resumes a run
from the content it restores (the port shape of the harness host ports).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

from arbora.agent_runs import NativeRunContext, current_native_run
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH


class CheckpointUnavailable(RuntimeError):
    """The engine cannot restore or save the checkpoint; nothing resumes from it."""


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class EngineHostCheckpoint:
    """One checkpoint transaction for an admitted native run."""

    def __init__(self, client: Any, owner: NativeRunContext) -> None:
        self._client = client
        self._owner = owner
        self._graph = owner.owner_graph or CONTROL_GRAPH
        self._revision: int | None = None
        self._known = False
        claim = owner.claim
        self._fence = {
            "worker_id": claim["lease_holder_ref"],
            "lease_epoch": claim["lease_epoch"],
            "fencing_token": claim["fencing_token"],
        }
        self.admission_identity = _digest(
            json.dumps([owner.run_id, owner.work_item_id, *self._fence.values()])
        )

    async def restore(self) -> str | None:
        """The last saved content, or ``None`` when this run has none."""
        from medulla.generated.coordination import (
            decode_get_run_checkpoint,
            send_get_run_checkpoint,
        )

        owner = self._owner
        try:
            answer = await send_get_run_checkpoint(
                self._client,
                {
                    "request": {
                        "context_id": owner.context_id,
                        "current_claim": self._fence,
                        "run_id": owner.run_id,
                        "tenant": owner.session.tenant,
                        "work_item_id": owner.work_item_id,
                    }
                },
                self._graph,
            )
            read = decode_get_run_checkpoint(answer)
        except Exception:
            raise CheckpointUnavailable("checkpoint restore failed") from None
        self._known = True
        if read is None:
            self._revision = None
            return None
        if _digest(read.content) != read.payload_digest:
            raise CheckpointUnavailable("checkpoint content does not match its digest")
        self._revision = read.revision
        return read.content

    async def save(self, content: str) -> None:
        """Save ``content`` as the next revision; a concurrent writer conflicts."""
        from medulla.generated.coordination import (
            decode_publish_run_checkpoint,
            send_publish_run_checkpoint,
        )

        if not self._known:
            await self.restore()
        owner = self._owner
        digest = _digest(content)
        key = _digest(json.dumps([self.admission_identity, self._revision, digest]))
        request = {
            "context_id": owner.context_id,
            "expected_revision": self._revision,
            "idempotency_key": key,
            "run_id": owner.run_id,
            "snapshot": {
                "content": content,
                "long_term": [],
                "payload_digest": digest,
                "short_term": [],
            },
            "tenant": owner.session.tenant,
            "work_item_id": owner.work_item_id,
            **self._fence,
        }
        try:
            answer = await send_publish_run_checkpoint(
                self._client, {"request": request}, self._graph, idempotency_key=key
            )
            receipt = decode_publish_run_checkpoint(answer)
        except Exception:
            raise CheckpointUnavailable("checkpoint save failed") from None
        self._revision = receipt.revision


def checkpoint_store(
    engine_provider: Callable[[], Any],
) -> Callable[[Any, Any], contextlib.AbstractAsyncContextManager[Any]]:
    """The host's checkpoint binder: ``(request, plan)`` to a port, or ``None``.

    It binds only inside the trusted native run scope of the claimed WorkItem.
    Outside it, or without an engine, no checkpoint is bound and the harness
    refuses a current-run restore rather than starting a replacement run.
    """

    @contextlib.asynccontextmanager
    async def bind(
        request: Any, plan: Any
    ) -> AsyncIterator[EngineHostCheckpoint | None]:
        del plan
        owner = current_native_run(request)
        engine = engine_provider()
        if owner is None or owner.owner_graph is None or engine is None:
            yield None
            return
        client = engine.graph_compute.for_graph(owner.owner_graph).async_client
        yield EngineHostCheckpoint(client, owner)

    return bind
