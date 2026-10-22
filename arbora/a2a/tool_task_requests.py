"""Canonical tool-task bytes in EG's existing blob and WorkItem owners.

This transports admitted data, not authority. Reading the original reference
does not reconstruct a caller credential or authorize execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from arbora import work_items
from arbora.api.contracts.session import resolve_session
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
from arbora.mcp_server.served_engine import _run_blocking

from .tool_task import (
    ToolTaskCarrier,
    ToolTaskRefused,
    ToolTaskRequest,
    tool_task_work_item_id,
)

_SCHEMA = "arbora-tool-task-request-v1"
_MAX_BYTES = 80_000
_BLOB_DIGEST = re.compile(r"[0-9a-f]{64}")


def _request_bytes(request: ToolTaskRequest) -> bytes:
    raw = json.dumps(
        request.model_dump(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > _MAX_BYTES:
        raise ToolTaskRefused("tool_task_request_too_large")
    return raw


class EgToolTaskRequestStore:
    """Store and re-read exact requests under the ambient original caller.

    The blob holder is durable and idempotent in EG. A failed or uncertain
    WorkItem submission leaves that holder intact: cleanup must not remove bytes
    a committed job might reference. This class never releases shared holders.
    """

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    @staticmethod
    def _caller(tenant: str, *, write: bool = False) -> str:
        session = resolve_session(required_scope="kg:write" if write else "kg:read")
        principal = session.actor.actor_id
        if session.tenant != tenant or not isinstance(principal, str) or not principal:
            raise ToolTaskRefused("tool_task_caller_mismatch")
        return principal

    def _blob(self) -> Any:
        return self._engine.graph_compute.for_graph(CONTROL_GRAPH).async_client.blob

    def store(self, job_id: str, request: ToolTaskRequest) -> ToolTaskCarrier:
        """Persist exact bytes and their locator before returning a carrier.

        The caller must already have admitted this request. This method checks
        its storage ownership and does not approve the operation or its scopes.
        The session reference remains request data until a separate original-
        caller authority resolver validates it before execution.
        """
        if self._caller(request.tenant, write=True) != request.principal:
            raise ToolTaskRefused("tool_task_caller_mismatch")
        carrier = ToolTaskCarrier(
            job_id=job_id,
            payload_ref=request.request_ref,
            tenant=request.tenant,
            session_id=request.session_id,
            allowed_tool_subset=(request.operation_id,),
        )
        blob = self._blob()
        digest = _run_blocking(
            lambda: blob.store(_request_bytes(request), chunk_size=1 << 20)
        )
        if not isinstance(digest, str) or _BLOB_DIGEST.fullmatch(digest) is None:
            raise ToolTaskRefused("tool_task_blob_unavailable")
        _run_blocking(lambda: blob.incref(digest))
        if self._caller(request.tenant, write=True) != request.principal:
            raise ToolTaskRefused("tool_task_caller_mismatch")
        item_id = tool_task_work_item_id(job_id)
        submitted, _created = work_items.submit_work_item_atomic(
            self._engine,
            kind="tool_task",
            idempotency_key=job_id,
            work_item_id=item_id,
            tenant=request.tenant,
            payload_ref=request.request_ref,
            max_attempts=1,
            created_by=request.principal,
            metadata={
                "schema": _SCHEMA,
                "request_blob": digest,
                "request_principal": request.principal,
                "request_session": request.session_id,
            },
        )
        if submitted != item_id:
            raise ToolTaskRefused("tool_task_work_item_mismatch")
        # Native idempotency may return a previous item; never emit a carrier
        # until the stored record and immutable bytes match this request.
        self.resolve(
            request.request_ref,
            job_id=job_id,
            tenant=request.tenant,
            session_id=request.session_id,
        )
        return carrier

    def resolve(
        self, request_ref: str, *, job_id: str, tenant: str, session_id: str
    ) -> ToolTaskRequest:
        """Read a job's native locator, then validate its bounded immutable bytes."""
        principal = self._caller(tenant)
        item = work_items.get_work_item(self._engine, tool_task_work_item_id(job_id))
        if (
            not isinstance(item, Mapping)
            or item.get("work_item_id") != tool_task_work_item_id(job_id)
            or item.get("kind") != "tool_task"
            or item.get("input_ref") != request_ref
        ):
            raise ToolTaskRefused("tool_task_work_item_mismatch")
        metadata = item.get("metadata")
        if (
            not isinstance(metadata, Mapping)
            or metadata.get("schema") != _SCHEMA
            or metadata.get("request_principal") != principal
            or metadata.get("request_session") != session_id
        ):
            raise ToolTaskRefused("tool_task_request_mismatch")
        digest = metadata.get("request_blob")
        if not isinstance(digest, str) or _BLOB_DIGEST.fullmatch(digest) is None:
            raise ToolTaskRefused("tool_task_blob_unavailable")
        raw = _run_blocking(lambda: self._read_bounded(digest))
        try:
            request = ToolTaskRequest.model_validate_json(raw)
        except ValueError:
            raise ToolTaskRefused("tool_task_request_mismatch") from None
        if (
            request.request_ref != request_ref
            or request.tenant != tenant
            or request.principal != principal
            or request.session_id != session_id
            or _request_bytes(request) != raw
        ):
            raise ToolTaskRefused("tool_task_request_mismatch")
        if self._caller(tenant) != principal:
            raise ToolTaskRefused("tool_task_caller_mismatch")
        return request

    async def _read_bounded(self, digest: str) -> bytes:
        blob = self._blob()
        cursor, count = await blob.fetch_begin(digest)
        try:
            # A bounded request written by store() always occupies one chunk.
            if type(count) is not int or count != 1:
                raise ToolTaskRefused("tool_task_request_too_large")
            raw = await blob.chunk_get(cursor, 0)
            if not isinstance(raw, bytes) or len(raw) > _MAX_BYTES:
                raise ToolTaskRefused("tool_task_request_too_large")
            return raw
        finally:
            await blob.fetch_end(cursor)
