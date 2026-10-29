"""Served source read operations (ARBORA-OPS-R848.1).

``source.tree``, ``source.blob``, ``source.history`` and ``source.blame``
serve the engine's branch-aware source projection (one repository's
``Branch`` node, its live ``FileVersion`` memberships and their ``Blob``
content) under the caller's own tenant and path ACL. Each operation is one
typed, read-only tool: admission is per principal (the exact engine scopes
the read uses) and per path (the engine's row-level visibility over the
requested path). An uncomposed reader fails closed with a typed
``UNAVAILABLE`` refusal, matching :mod:`arbora.api.ops.retrieval`.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.ops._common import Params, bound_service
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)

_NAME_MAX = 1024
_PATH_MAX = 1024


def _decode_blob_content(content: Any, encoding: str, *, explicit: bool) -> Any:
    if isinstance(content, bytearray):
        content = bytes(content)
    if isinstance(content, str) and encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("invalid base64 source content") from exc
    if isinstance(content, str):
        return content.encode("utf-8")
    if isinstance(content, bytes) and encoding == "utf-8" and explicit:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("invalid UTF-8 source content") from exc
    return content


def _blob_content_encoding(content: bytes) -> Literal["utf-8", "base64"]:
    try:
        content.decode("utf-8")
    except UnicodeDecodeError:
        return "base64"
    return "utf-8"


def _portable_path(value: str) -> str:
    """Refuse paths that cannot name a repository file portably."""
    if value and (
        value.startswith("/")
        or value.endswith("/")
        or value != value.strip()
        or ".." in value.split("/")
        or "//" in value
    ):
        raise ValueError("path must be a portable relative path")
    return value


class _SourceRepoParams(Params):
    """One repository and one ref, named by their portable ids."""

    repository: str = Field(min_length=1, max_length=_NAME_MAX)
    ref: str = Field(min_length=1, max_length=_NAME_MAX)


class SourceTreeParams(_SourceRepoParams):
    """List one directory of one ref; the empty path lists the repository root."""

    path: str = Field(default="", max_length=_PATH_MAX)

    _validate = field_validator("path")(_portable_path)


class SourceBlobParams(_SourceRepoParams):
    """Read one file of one ref."""

    path: str = Field(min_length=1, max_length=_PATH_MAX)

    _validate = field_validator("path")(_portable_path)


class _SourceRepoPathParams(Params):
    repository: str = Field(min_length=1, max_length=_NAME_MAX)
    path: str = Field(min_length=1, max_length=_PATH_MAX)

    _validate = field_validator("path")(_portable_path)


class SourceHistoryParams(_SourceRepoPathParams):
    """List the refs of one repository that carry one path."""


class SourceBlameParams(SourceBlobParams):
    """Attribute one file to its last-touch revision on one ref."""


class SourceTreeEntry(BaseModel):
    """One immediate child of the listed directory.

    Files carry their content digest and the membership edge's last-touch
    revision; directories are derived from the file paths (the projection
    stores no directory nodes) and carry neither.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    kind: str
    content_digest: str | None = None
    revision_id: str | None = None


class SourceTreeOutcome(BaseModel):
    """The immediate children of one directory, in path order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    repository: str
    ref: str
    path: str
    found: bool
    reason: str | None = None
    entries: tuple[SourceTreeEntry, ...] = ()


class SourceBlobOutcome(BaseModel):
    """One file's content, CAS manifest reference, and returned-byte hash."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    repository: str
    ref: str
    path: str
    found: bool
    reason: str | None = None
    content_digest: str | None = None
    content_sha256: str | None = None
    content: bytes = b""
    content_encoding: Literal["utf-8", "base64"] = "utf-8"
    size: int = 0

    @model_validator(mode="before")
    @classmethod
    def decode_wire_content(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        encoding = value.get("content_encoding", "utf-8")
        if not isinstance(encoding, str) or encoding not in {"utf-8", "base64"}:
            raise ValueError("unknown source content encoding")
        content = _decode_blob_content(
            value.get("content", b""), encoding, explicit="content_encoding" in value
        )
        if isinstance(content, bytes):
            value["content"] = content
            value["content_encoding"] = _blob_content_encoding(content)
        return value

    @field_serializer("content", when_used="json")
    def serialize_content(self, content: bytes) -> str:
        if self.content_encoding == "base64":
            return base64.b64encode(content).decode("ascii")
        return content.decode("utf-8")


class SourceHistoryEntry(BaseModel):
    """One ref's last touch of one path."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: str
    revision_id: str
    content_digest: str | None = None


class SourceHistoryOutcome(BaseModel):
    """Every ref carrying one path, in ref order."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    repository: str
    path: str
    entries: tuple[SourceHistoryEntry, ...] = ()


class SourceBlameOutcome(BaseModel):
    """One file's last-touch provenance on one ref.

    ``line_level`` is always ``False`` on the current engine substrate: the
    projection records a per-file last-touch revision, never line-level
    attribution. The field exists so a caller cannot mistake file-level
    blame for line-level blame.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    repository: str
    ref: str
    path: str
    found: bool
    reason: str | None = None
    revision_id: str | None = None
    content_digest: str | None = None
    line_level: bool = False


class SourceTreeResult(Params):
    value: dict[str, Any]


class SourceBlobResult(Params):
    value: dict[str, Any]


class SourceHistoryResult(Params):
    value: dict[str, Any]


class SourceBlameResult(Params):
    value: dict[str, Any]


@runtime_checkable
class SourceReader(Protocol):
    """The one typed port Arbora serves the source reads through."""

    async def tree(
        self,
        *,
        repository: str,
        ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceTreeOutcome: ...

    async def blob(
        self,
        *,
        repository: str,
        ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceBlobOutcome: ...

    async def history(
        self, *, repository: str, path: str, context: ExecutionContext
    ) -> SourceHistoryOutcome: ...

    async def blame(
        self,
        *,
        repository: str,
        ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceBlameOutcome: ...


def _reader(context: Any) -> SourceReader:
    return bound_service(
        context, "source_reader", reason="source reader is not composed"
    )


def _value(outcome: BaseModel) -> dict[str, Any]:
    return {"value": outcome.model_dump(mode="json")}


async def handle_source_tree(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the immediate children of one directory on one ref."""
    request = SourceTreeParams.model_validate(dict(params))
    outcome = await _reader(context).tree(
        repository=request.repository,
        ref=request.ref,
        path=request.path,
        context=context,
    )
    return _value(outcome)


async def handle_source_blob(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one file's content on one ref."""
    request = SourceBlobParams.model_validate(dict(params))
    outcome = await _reader(context).blob(
        repository=request.repository,
        ref=request.ref,
        path=request.path,
        context=context,
    )
    return _value(outcome)


async def handle_source_history(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return every ref of one repository that carries one path."""
    request = SourceHistoryParams.model_validate(dict(params))
    outcome = await _reader(context).history(
        repository=request.repository,
        path=request.path,
        context=context,
    )
    return _value(outcome)


async def handle_source_blame(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one file's last-touch provenance on one ref."""
    request = SourceBlameParams.model_validate(dict(params))
    outcome = await _reader(context).blame(
        repository=request.repository,
        ref=request.ref,
        path=request.path,
        context=context,
    )
    return _value(outcome)


def _read_op(
    *,
    op_id: str,
    summary: str,
    examples: tuple[str, ...],
    params: type[BaseModel],
    result: type[BaseModel],
    handler: str,
    scopes: frozenset[str],
) -> OpSpec:
    """One read-only composite op; the scopes are the engine reads it uses."""
    return OpSpec(
        id=op_id,
        verb=Verb.FIND,
        summary=summary,
        examples=examples,
        params=params,
        result=result,
        binding=Composite(handler=handler),
        scopes=scopes,
        effect=Effect.READ,
        idempotency=Idempotency.NONE,
        audit=AuditClass.NONE,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _read_op(
            op_id="source.tree",
            summary="List one directory of one repository ref",
            examples=("list the files under src on main",),
            params=SourceTreeParams,
            result=SourceTreeResult,
            handler="arbora.api.ops.source.handle_source_tree",
            scopes=frozenset({"node:read", "edge:read"}),
        ),
        _read_op(
            op_id="source.blob",
            summary="Read one file of one repository ref",
            examples=("read the file at src/app.py on main",),
            params=SourceBlobParams,
            result=SourceBlobResult,
            handler="arbora.api.ops.source.handle_source_blob",
            scopes=frozenset({"node:read", "edge:read", "blob:read"}),
        ),
        _read_op(
            op_id="source.history",
            summary="List the refs of one repository that carry one path",
            examples=("which refs carry src/app.py",),
            params=SourceHistoryParams,
            result=SourceHistoryResult,
            handler="arbora.api.ops.source.handle_source_history",
            scopes=frozenset({"node:read", "edge:read"}),
        ),
        _read_op(
            op_id="source.blame",
            summary="Attribute one file to its last-touch revision on one ref",
            examples=("what last touched src/app.py on main",),
            params=SourceBlameParams,
            result=SourceBlameResult,
            handler="arbora.api.ops.source.handle_source_blame",
            scopes=frozenset({"node:read", "edge:read"}),
        ),
    )


specs = operations

__all__ = [
    "SourceBlobOutcome",
    "SourceBlobParams",
    "SourceBlobResult",
    "SourceBlameOutcome",
    "SourceBlameParams",
    "SourceBlameResult",
    "SourceHistoryEntry",
    "SourceHistoryOutcome",
    "SourceHistoryParams",
    "SourceHistoryResult",
    "SourceReader",
    "SourceTreeEntry",
    "SourceTreeOutcome",
    "SourceTreeParams",
    "SourceTreeResult",
    "handle_source_blob",
    "handle_source_blame",
    "handle_source_history",
    "handle_source_tree",
    "operations",
    "specs",
]
