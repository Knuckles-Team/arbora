"""Source adapter to EG's existing owner-bound original run-admission read.

No native deployment or token is created here. The server's anonymous principal
and immutable run-owner binding, not caller metadata, own this read decision.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from medulla.client import MedullaClient
from medulla.generated.coordination import (
    decode_get_run_work_item,
    send_get_run_work_item,
)

from arbora.core.work_item_status import WorkItemStatus

from .anonymous import (
    AnonymousCaller,
    AnonymousNativeReadPort,
    AnonymousReadPolicy,
    AnonymousTaskReadOwner,
    AnonymousTaskStatus,
)

_audit_log = logging.getLogger("arbora.a2a.anonymous")

Audit = Callable[[Mapping[str, str]], None]


class AnonymousRefused(PermissionError):
    """A typed refusal of a native anonymous read (ARBORA-IDENTITY-R004.8.2).

    ``code`` is one of ``ANON_OWNER_MISMATCH``, ``ANON_NAMESPACE_MISMATCH`` and
    ``ANON_CLIENT_SUBSTITUTED``; the public answer stays the one 403.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def log_audit(event: Mapping[str, str]) -> None:
    """The default audit sink: one structured record per refusal, never a credential."""
    _audit_log.info("a2a.anonymous.refused", extra={"audit": dict(event)})


class EgAnonymousTaskReadPort(AnonymousNativeReadPort):
    """Use GetRunWorkItem on an already-configured local no-auth client only."""

    def __init__(
        self, client: MedullaClient, *, graph: str, audit: Audit = log_audit
    ) -> None:
        if (
            type(client) is not MedullaClient
            or type(graph) is not str
            or not graph
            or len(graph) > 128
        ):
            raise ValueError("Exact local native client and graph required")
        self._client = client
        self._graph = graph
        self._audit = audit
        self._check_mode()

    def _check_mode(self, task_id: str = "") -> None:
        if self._client.auth_mode != "none" or self._client.authenticated is not False:
            raise self._refuse(
                "ANON_CLIENT_SUBSTITUTED",
                task_id,
                "Explicit native anonymous mode required",
            )

    def _refuse(self, code: str, task_id: str, message: str) -> AnonymousRefused:
        """Audit one refusal (task id and reason code only) and return it to raise."""
        self._audit({"task_id": task_id, "reason": code})
        return AnonymousRefused(code, message)

    def _require_local(self, caller: AnonymousCaller) -> str:
        """The caller's task id, refused ``ANON_NAMESPACE_MISMATCH`` off the local namespace."""
        task_id = str(getattr(caller, "task_id", ""))
        if type(caller) is not AnonymousCaller or caller.tenant != "local":
            raise self._refuse(
                "ANON_NAMESPACE_MISMATCH",
                task_id,
                "Native anonymous namespace is local only",
            )
        return task_id

    async def get(self, caller: AnonymousCaller) -> AnonymousTaskStatus | None:
        self._check_mode(str(getattr(caller, "task_id", "")))
        task_id = self._require_local(caller)
        if await self._client.supports("GetRunWorkItem") is not True:
            raise RuntimeError("Native owner-bound reads unavailable")
        self._check_mode(task_id)
        result = await send_get_run_work_item(
            self._client,
            {"tenant": "local", "run_id": caller.task_id},
            graph=self._graph,
        )
        decode_get_run_work_item(result)
        value = result.payload
        self._check_mode(task_id)
        if value is None:
            return None
        return self._project(value, caller)

    def _bind(self, value: Any, task_id: str) -> None:
        """Refuse a read whose immutable admission is not this local task's own."""
        admission = value["admission"]
        context = admission["context"]
        if context["tenant_id"] != "local" or context["graph"] != self._graph:
            raise self._refuse(
                "ANON_NAMESPACE_MISMATCH",
                task_id,
                "Native anonymous namespace mismatch",
            )
        item = value["work_item"]
        if (
            admission["run_id"] != task_id
            or admission["work_item_id"] is not None
            or admission["kind"] != "orchestrator_task"
            or item["kind"] != "orchestrator_task"
        ):
            raise self._refuse(
                "ANON_OWNER_MISMATCH", task_id, "Native anonymous run binding mismatch"
            )

    def _project(self, value: Any, caller: AnonymousCaller) -> AnonymousTaskStatus:
        self._bind(value, caller.task_id)
        admission = value["admission"]
        context = admission["context"]
        item = value["work_item"]
        # GetRunWorkItem independently binds the transport's server-created
        # anonymous principal to the immutable native owner. Subject/agent/scopes
        # in original request context are provenance, never used as grants here.
        owner_ref = "anonymous:sha256:" + hashlib.sha256(b"local").hexdigest()
        return AnonymousTaskStatus(
            work_item_id=item["work_item_id"],
            run_id=admission["run_id"],
            tenant=context["tenant_id"],
            owner_ref=owner_ref,
            status=WorkItemStatus(item["status"]),
            authority_mode="none",
            context_id=admission["metadata"]["a2a_context_id"],
        )


async def compose_anonymous_task_reads(
    client: MedullaClient,
    *,
    graph: str,
    policy: AnonymousReadPolicy,
    audit: Audit = log_audit,
) -> AnonymousTaskReadOwner | None:
    """The none-mode owner of anonymous task reads, or ``None`` when it must not compose.

    It composes only for a client in explicit none mode that reports ``GetRunWorkItem``
    support; any other mode or a release without the method composes nothing, and there
    is no enabling switch (ARBORA-IDENTITY-R004.8.2).
    """
    if client.auth_mode != "none" or client.authenticated is not False:
        return None
    if await client.supports("GetRunWorkItem") is not True:
        return None
    return AnonymousTaskReadOwner(
        mode="none",
        policy=policy,
        work_items=EgAnonymousTaskReadPort(client, graph=graph, audit=audit),
    )
