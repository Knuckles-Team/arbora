"""Canonical tool-task bytes in EG's existing blob and WorkItem owners.

This transports admitted data, not authority. Reading the original reference
does not reconstruct a caller credential or authorize execution.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from pydantic_core import to_jsonable_python

from arbora import work_items
from arbora.epistemic import run_blocking
from arbora.security.session import resolve_session
from arbora.semantic_provisioning import CONTROL_GRAPH

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
        digest = run_blocking(
            lambda: blob.store(_request_bytes(request), chunk_size=1 << 20)
        )
        if not isinstance(digest, str) or _BLOB_DIGEST.fullmatch(digest) is None:
            raise ToolTaskRefused("tool_task_blob_unavailable")
        run_blocking(lambda: blob.incref(digest))
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
                "request_operation": request.operation_id,
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
        self,
        request_ref: str,
        *,
        job_id: str,
        tenant: str,
        session_id: str,
        service: bool = False,
    ) -> ToolTaskRequest:
        """Read a job's native locator, then validate its bounded immutable bytes.

        A ``service`` read is the host consumer's: the ambient session is the
        host's own authority, so the stored request principal is the one the
        record names. It grants nothing; the original caller is proven by the
        delegation read before any effect (``ARBORA-A2A-R002.2.3.3``).
        """
        session_principal = self._caller(tenant)
        metadata = self._native_metadata(request_ref, job_id)
        principal = metadata.get("request_principal") if service else session_principal
        if (
            metadata.get("schema") != _SCHEMA
            or metadata.get("request_principal") != principal
            or metadata.get("request_session") != session_id
        ):
            raise ToolTaskRefused("tool_task_request_mismatch")
        digest = metadata.get("request_blob")
        if not isinstance(digest, str) or _BLOB_DIGEST.fullmatch(digest) is None:
            raise ToolTaskRefused("tool_task_blob_unavailable")
        raw = run_blocking(lambda: self._read_bounded(digest))
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
        if self._caller(tenant) != session_principal:
            raise ToolTaskRefused("tool_task_caller_mismatch")
        return request

    def _native_metadata(self, request_ref: str, job_id: str) -> Mapping[str, Any]:
        """The WorkItem metadata of the job, once its locator matches the request."""
        item_id = tool_task_work_item_id(job_id)
        item = work_items.get_work_item(self._engine, item_id)
        if (
            not isinstance(item, Mapping)
            or item.get("work_item_id") != item_id
            or item.get("kind") != "tool_task"
            or item.get("input_ref") != request_ref
        ):
            raise ToolTaskRefused("tool_task_work_item_mismatch")
        metadata = item.get("metadata")
        if not isinstance(metadata, Mapping):
            raise ToolTaskRefused("tool_task_request_mismatch")
        return metadata

    def store_result(self, job_id: str, value: Any) -> str:
        """Store one job's full result in the blob holder and return its reference.

        The reference is content-addressed and the holder durable, like the
        request bytes: a repeat store of the same result names the same bytes.
        """
        raw = json.dumps(
            {"job_id": job_id, "value": to_jsonable_python(value)},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        blob = self._blob()
        digest = run_blocking(lambda: blob.store(raw, chunk_size=1 << 20))
        if not isinstance(digest, str) or _BLOB_DIGEST.fullmatch(digest) is None:
            raise ToolTaskRefused("tool_task_blob_unavailable")
        run_blocking(lambda: blob.incref(digest))
        return f"tool-task-result:sha256:{digest}"

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
