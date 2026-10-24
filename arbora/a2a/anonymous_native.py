"""Source adapter to EG's existing owner-bound original run-admission read.

No native deployment or token is created here. The server's anonymous principal
and immutable run-owner binding, not caller metadata, own this read decision.
"""

from __future__ import annotations

import hashlib
from typing import Any

from epistemic_graph.client import EpistemicGraphClient
from epistemic_graph.generated.coordination import (
    decode_get_run_work_item,
    send_get_run_work_item,
)

from .anonymous import AnonymousCaller, AnonymousNativeReadPort, AnonymousTaskStatus


class EgAnonymousTaskReadPort(AnonymousNativeReadPort):
    """Use GetRunWorkItem on an already-configured local no-auth client only."""

    def __init__(self, client: EpistemicGraphClient, *, graph: str) -> None:
        if (
            type(client) is not EpistemicGraphClient
            or type(graph) is not str
            or not graph
            or len(graph) > 128
        ):
            raise ValueError("Exact local native client and graph required")
        self._client = client
        self._graph = graph
        self._check_mode()

    def _check_mode(self) -> None:
        if self._client.auth_mode != "none" or self._client.authenticated is not False:
            raise PermissionError("Explicit native anonymous mode required")

    async def get(self, caller: AnonymousCaller) -> AnonymousTaskStatus | None:
        self._check_mode()
        if type(caller) is not AnonymousCaller or caller.tenant != "local":
            raise PermissionError("Native anonymous namespace is local only")
        if await self._client.supports("GetRunWorkItem") is not True:
            raise RuntimeError("Native owner-bound reads unavailable")
        self._check_mode()
        result = await send_get_run_work_item(
            self._client,
            {"tenant": "local", "run_id": caller.task_id},
            graph=self._graph,
        )
        decode_get_run_work_item(result)
        value = result.payload
        self._check_mode()
        if value is None:
            return None
        return self._project(value, caller)

    def _project(self, value: Any, caller: AnonymousCaller) -> AnonymousTaskStatus:
        admission = value["admission"]
        context = admission["context"]
        item = value["work_item"]
        if (
            admission["run_id"] != caller.task_id
            or context["tenant_id"] != "local"
            or context["graph"] != self._graph
            or admission["work_item_id"] is not None
            or admission["kind"] != "orchestrator_task"
            or item["kind"] != "orchestrator_task"
        ):
            raise PermissionError("Native anonymous run binding mismatch")
        # GetRunWorkItem independently binds the transport's server-created
        # anonymous principal to the immutable native owner. Subject/agent/scopes
        # in original request context are provenance, never used as grants here.
        owner_ref = "anonymous:sha256:" + hashlib.sha256(b"local").hexdigest()
        return AnonymousTaskStatus(
            work_item_id=item["work_item_id"],
            run_id=admission["run_id"],
            tenant=context["tenant_id"],
            owner_ref=owner_ref,
            status=item["status"],
            authority_mode="none",
            context_id=admission["metadata"]["a2a_context_id"],
        )
