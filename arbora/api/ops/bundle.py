"""Knowledge bundle operations (ARBORA-OPS-R923).

``bundle.export`` and ``bundle.import`` forward the engine's ``BundleExport`` and
``BundleImport`` (``MEDULLA-DURABLE-KERNEL-R877``); ``bundle.download`` and
``bundle.upload`` are the chunk operations the transfer routes
(``arbora/api/http/bundles.py``) drive through the same invocation chokepoint. Arbora
runs no export logic and holds no bundle bytes: each chunk is read from or staged into
the engine blob store (``BlobFetchBegin``, ``BlobChunkGet``, ``BlobBegin``,
``BlobChunkPut``, ``BlobCommit``). The bundle methods are engine ``main`` shapes the pinned
release lacks, so they go through :func:`arbora.api.ops.pin_guards.send_main` and refuse
``UNAVAILABLE`` naming the missing method until the re-pin.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import Field, model_validator

from arbora.api.errors import EngineRefusal
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.ops.pin_guards import send_main
from arbora.api.registry import Confirm, OpSpec, Verb

READ_SCOPE = "graph:read"
WRITE_SCOPE = "graph:write"
#: The most one bundle transfer carries (4 GiB); a larger body is ``BUNDLE_TOO_LARGE``.
MAX_BUNDLE_BYTES = 4 * 1024**3
#: The bytes of one transfer chunk.
CHUNK_BYTES = 1024 * 1024

_REF = Field(min_length=1, max_length=256)
_DIGEST = r"^[0-9a-f]{64}$"


class BundleExportParams(Params):
    graphs: list[str] = Field(min_length=1, max_length=32)
    include_embeddings: bool = False
    include_documents: bool = False

    @model_validator(mode="after")
    def _graphs_not_blank(self) -> BundleExportParams:
        if any(not name.strip() for name in self.graphs):
            raise ValueError("a bundle graph name is not blank")
        return self


class BundleExportResult(Params):
    bundle_ref: str = _REF
    manifest_digest: str = Field(pattern=_DIGEST)
    size_bytes: int = Field(ge=0)


class BundleImportParams(Params):
    bundle_ref: str = _REF
    target_graph_prefix: str = Field(default="", max_length=256)
    on_conflict: Literal["refuse", "skip"] = "refuse"


class BundleImportResult(Params):
    receipt_ref: str = Field(pattern=_DIGEST)
    triples: int = Field(ge=0)
    documents: int = Field(ge=0)


class BundleDownloadParams(Params):
    bundle_ref: str = _REF
    index: int = Field(default=0, ge=0)


class BundleDownloadResult(Params):
    chunks: int = Field(ge=0)
    data_b64: str


class BundleUploadPhase(StrEnum):
    """One step of a staged bundle upload."""

    BEGIN = "begin"
    CHUNK = "chunk"
    COMMIT = "commit"


class BundleUploadParams(Params):
    """One staging step: ``begin`` opens a blob, ``chunk`` appends, ``commit`` seals it."""

    phase: BundleUploadPhase
    cursor: int | None = Field(default=None, ge=0)
    data_b64: str | None = None

    @model_validator(mode="after")
    def _phase_fields(self) -> BundleUploadParams:
        needs_cursor = self.phase != BundleUploadPhase.BEGIN
        if needs_cursor != (self.cursor is not None):
            raise ValueError("a cursor is given for chunk and commit only")
        if (self.phase == BundleUploadPhase.CHUNK) != (self.data_b64 is not None):
            raise ValueError("data is given for chunk only")
        return self


class BundleUploadResult(Params):
    cursor: int | None = None
    bundle_ref: str | None = None


def _mapping(answer: Any) -> Mapping[str, Any]:
    if not isinstance(answer, Mapping):
        raise unavailable("engine result malformed")
    return answer


def _field(answer: Any, name: str) -> Any:
    try:
        return _mapping(answer)[name]
    except KeyError:
        raise unavailable("engine result malformed") from None


async def handle_bundle_export(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    receipt = await send_main(
        context,
        "BundleExport",
        {
            "graphs": list(params["graphs"]),
            "include_embeddings": bool(params["include_embeddings"]),
            "include_documents": bool(params["include_documents"]),
        },
    )
    if _field(receipt, "size_bytes") > MAX_BUNDLE_BYTES:
        from arbora.api.invoke.pipeline import OperationRefused

        raise OperationRefused("BUNDLE_TOO_LARGE", {})
    return {
        "bundle_ref": _field(receipt, "bundle_ref"),
        "manifest_digest": _field(receipt, "manifest_digest"),
        "size_bytes": _field(receipt, "size_bytes"),
    }


async def handle_bundle_import(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Import one staged bundle. The engine's own codes reach the caller unchanged."""
    receipt = await send_main(
        context,
        "BundleImport",
        {
            "bundle_ref": params["bundle_ref"],
            "target_graph_prefix": params["target_graph_prefix"],
            "on_conflict": params["on_conflict"],
        },
        idempotent=True,
    )
    body = json.dumps(_mapping(receipt), sort_keys=True, separators=(",", ":"))
    return {
        "receipt_ref": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "triples": _field(receipt, "triples"),
        "documents": _field(receipt, "documents"),
    }


def _absent() -> Any:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused("NOT_FOUND", {})


async def _chunk_bytes(context: Any, cursor: int, index: int) -> bytes:
    """One blob chunk as the engine sent it: raw bytes, never decoded as MessagePack."""
    from medulla import EngineResponseError
    from medulla.generated import send_by_id

    from arbora.api.engine_errors import ENGINE_ERRORS
    from arbora.api.invoke.bootstrap import caller_dispatch_graph
    from arbora.core.claims import claim_values
    from arbora.security.anonymous import client_scope

    graph = await caller_dispatch_graph(context)
    with client_scope(context.client, claim_values(context.caller.engine_claims)):
        try:
            part = await send_by_id(
                context.client, "BlobChunkGet", {"cursor": cursor, "idx": index}, graph
            )
        except EngineResponseError as exc:
            code = exc.code if exc.code in ENGINE_ERRORS else "UNAVAILABLE"
            raise EngineRefusal(code) from exc
    return bytes(part) if isinstance(part, (bytes, bytearray, memoryview)) else b""


async def handle_bundle_download(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One chunk of a staged bundle. A foreign reference answers exactly like an absent one."""
    try:
        begun = await engine_send(
            context, "BlobFetchBegin", {"digest": params["bundle_ref"]}
        )
    except EngineRefusal:
        raise _absent() from None
    if not isinstance(begun, (list, tuple)) or len(begun) != 2:
        raise unavailable("engine result malformed")
    cursor, chunks = begun
    try:
        if params["index"] >= chunks:
            raise _absent()
        data = await _chunk_bytes(context, cursor, params["index"])
    finally:
        await engine_send(context, "BlobFetchEnd", {"cursor": cursor})
    return {"chunks": chunks, "data_b64": base64.b64encode(data).decode("ascii")}


async def handle_bundle_upload(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Stage one chunk step of an uploaded bundle; commit answers the ``bundle_ref``."""
    phase = params["phase"]
    if phase == "begin":
        begun = await engine_send(
            context, "BlobBegin", {"chunk_size": CHUNK_BYTES}, idempotent=True
        )
        cursor = begun.get("cursor") if isinstance(begun, Mapping) else begun
        if type(cursor) is not int:
            raise unavailable("engine result malformed")
        return {"cursor": cursor}
    if phase == "chunk":
        data = base64.b64decode(params["data_b64"], validate=True)
        await engine_send(
            context,
            "BlobChunkPut",
            {"cursor": params["cursor"], "data": data},
            idempotent=True,
        )
        return {"cursor": params["cursor"]}
    sealed = await engine_send(
        context, "BlobCommit", {"cursor": params["cursor"]}, idempotent=True
    )
    return {"bundle_ref": _field(sealed, "digest")}


#: Writes replay under the engine method that holds the record, never here.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "bundle.import": "medulla:BundleImport",
        "bundle.upload": "medulla:BlobCommit",
    }
)


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.bundle"
    return (
        build_read_op(
            op_id="bundle.export",
            summary="Export graphs as one knowledge bundle held by the engine",
            examples=("export these graphs as a bundle",),
            params=BundleExportParams,
            result=BundleExportResult,
            handler=f"{module}.handle_bundle_export",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="bundle.import",
            summary="Import a staged knowledge bundle into the caller's tenant",
            examples=("import this bundle",),
            params=BundleImportParams,
            result=BundleImportResult,
            handler=f"{module}.handle_bundle_import",
            scope=WRITE_SCOPE,
            verb=Verb.WRITE,
            confirm=Confirm.PLAN,
        ),
        build_read_op(
            op_id="bundle.download",
            summary="Read one chunk of a staged bundle",
            examples=("download this bundle",),
            params=BundleDownloadParams,
            result=BundleDownloadResult,
            handler=f"{module}.handle_bundle_download",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="bundle.upload",
            summary="Stage one chunk step of an uploaded bundle",
            examples=("upload a bundle",),
            params=BundleUploadParams,
            result=BundleUploadResult,
            handler=f"{module}.handle_bundle_upload",
            scope=WRITE_SCOPE,
            verb=Verb.WRITE,
        ),
    )


specs = operations

__all__ = [
    "CHUNK_BYTES",
    "MAX_BUNDLE_BYTES",
    "NATIVE_IDEMPOTENCY",
    "handle_bundle_download",
    "handle_bundle_export",
    "handle_bundle_import",
    "handle_bundle_upload",
    "operations",
    "specs",
]
