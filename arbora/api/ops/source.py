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
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
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


_DIFF_PAGE = 1000


class SourceDiffParams(Params):
    """Diff two refs of one repository, optionally under one path prefix."""

    repository: str = Field(min_length=1, max_length=_NAME_MAX)
    base_ref: str = Field(min_length=1, max_length=_NAME_MAX)
    head_ref: str = Field(min_length=1, max_length=_NAME_MAX)
    path: str = Field(default="", max_length=4096)
    cursor: str | None = Field(default=None, min_length=1, max_length=4096)

    _validate = field_validator("path")(_portable_path)


class SourceDiffEntry(BaseModel):
    """One changed path between two refs; ``readable`` is the caller's read grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    change: Literal["added", "removed", "modified"]
    base_digest: str | None = None
    head_digest: str | None = None
    readable: bool = True


class SourceDiffOutcome(BaseModel):
    """The tree diff of two refs, in path order, one page at a time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant: str
    repository: str
    base_ref: str
    head_ref: str
    path: str
    found: bool
    reason: str | None = None
    entries: tuple[SourceDiffEntry, ...] = ()
    removed: int = 0
    next_cursor: str | None = None


class SourceDiffResult(Params):
    value: dict[str, Any]


class SymbolEditOpParams(Params):
    """One symbol-anchored edit of the ``SymbolEdit`` contract (MEDULLA-SCM-R013.1)."""

    symbol_id: str = Field(min_length=1, max_length=512)
    kind: Literal[
        "replace_symbol", "replace_body", "insert_before", "insert_after", "delete"
    ]
    anchor_span_digest: str = Field(min_length=1, max_length=128)
    new_text: str = Field(max_length=1_048_576)


class SymbolEditParams(Params):
    """Submit one graph-native symbol edit to one branch."""

    repo: str = Field(min_length=1, max_length=_NAME_MAX)
    branch: str = Field(min_length=1, max_length=_NAME_MAX)
    base_rev: str = Field(min_length=1, max_length=128)
    ops: list[SymbolEditOpParams] = Field(min_length=1, max_length=256)
    message: str = Field(min_length=1, max_length=8192)
    run_trace_id: str = Field(min_length=1, max_length=128)
    #: The committed ``GeneratedCodeAdmission`` record an agent principal cites (R869).
    admission_record_digest: str | None = Field(default=None, max_length=128)


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
    #: The ref's head revision as the engine's Branch projection holds it. Absent while
    #: the engine release serves none (pin-lag): a changeset base then cannot be read and
    #: the submission refuses closed (ARBORA-OPS-R894).
    head_revision: str | None = None
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

    async def diff(
        self,
        *,
        repository: str,
        base_ref: str,
        head_ref: str,
        path: str,
        context: ExecutionContext,
    ) -> SourceDiffOutcome: ...

    async def symbol_paths(
        self,
        *,
        repository: str,
        branch: str,
        symbol_ids: tuple[str, ...],
        context: ExecutionContext,
    ) -> Mapping[str, str | None]: ...


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


def _page(
    entries: tuple[SourceDiffEntry, ...], cursor: str | None
) -> tuple[list[dict[str, Any]], str | None]:
    """One cursor page of the readable entries: after ``cursor``, at most 1000."""
    later = [e for e in entries if cursor is None or e.path > cursor]
    page = later[:_DIFF_PAGE]
    more = len(later) > _DIFF_PAGE
    return (
        [e.model_dump(mode="json", exclude={"readable"}) for e in page],
        page[-1].path if more else None,
    )


async def handle_source_diff(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Diff two refs; an entry whose path the caller may not read is removed and counted."""
    request = SourceDiffParams.model_validate(dict(params))
    outcome = await _reader(context).diff(
        repository=request.repository,
        base_ref=request.base_ref,
        head_ref=request.head_ref,
        path=request.path,
        context=context,
    )
    readable = tuple(e for e in outcome.entries if e.readable)
    entries, cursor = _page(readable, request.cursor)
    value = outcome.model_dump(mode="json", exclude={"entries", "next_cursor"})
    value.update(
        entries=entries,
        removed=outcome.removed + len(outcome.entries) - len(readable),
        next_cursor=cursor,
    )
    return {"value": value}


async def handle_source_edit_symbol(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """``SubmitSymbolEdit`` once every touched file is one the caller may write.

    Each symbol resolves to its file through the ``SourceReader`` port. A symbol
    that does not resolve is refused exactly like an absent one, so a denied
    path discloses nothing. The engine accept, rebase receipt or refusal
    (``EDIT_BASE_STALE``, ``EDIT_OPS_OVERLAP``, ``EDIT_UNPARSEABLE``,
    ``EDIT_ROUNDTRIP_MISMATCH``) returns unchanged.
    """
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

    request = SymbolEditParams.model_validate(dict(params))
    try:
        require_methods("SubmitSymbolEdit")
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    edit = request.model_dump(mode="json")
    cited = edit.pop("admission_record_digest", None)
    await require_admission_record(context, edit, cited)
    symbols = tuple(dict.fromkeys(op_.symbol_id for op_ in request.ops))
    resolved = await _reader(context).symbol_paths(
        repository=request.repo,
        branch=request.branch,
        symbol_ids=symbols,
        context=context,
    )
    if any(resolved.get(symbol) is None for symbol in symbols):
        raise OperationRefused(
            "INVALID_ARGUMENT", {"reason": "a symbol is not an editable file symbol"}
        )
    answer = await engine_send(
        context,
        "SubmitSymbolEdit",
        {"request": edit},
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


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


_PATH_OPS = ("add", "modify", "delete", "rename")


class SubmittedBlob(Params):
    digest: str = Field(min_length=1, max_length=128)
    size: int = Field(ge=0)


class SubmittedPath(Params):
    """One changed path; the content is a blob already stored by digest."""

    path: str = Field(min_length=1, max_length=_PATH_MAX)
    op: Literal["add", "modify", "delete", "rename"]
    mode: int = Field(ge=0, le=0o177777)
    blob: SubmittedBlob | None = None
    renamed_from: str | None = Field(default=None, max_length=_PATH_MAX)

    _validate = field_validator("path")(_portable_path)

    @model_validator(mode="after")
    def _op_shape(self) -> SubmittedPath:
        if (self.op == "delete") != (self.blob is None):
            raise ValueError("a delete carries no blob and every other op carries one")
        if (self.op == "rename") != (self.renamed_from is not None):
            raise ValueError("only a rename names its source path")
        return self


class SourceTrailers(Params):
    fixes: list[str] = Field(default_factory=list, max_length=64)
    spec_ids: list[str] = Field(default_factory=list, max_length=256)
    withdraw: list[str] = Field(default_factory=list, max_length=64)
    withdraw_reason: str | None = Field(default=None, max_length=512)


class SourceCommitParams(Params):
    """Submit one changeset to one branch; the engine checks each path grant."""

    repository: str = Field(min_length=1, max_length=_NAME_MAX)
    branch: str = Field(min_length=1, max_length=_NAME_MAX)
    message: str = Field(min_length=1, max_length=8192)
    paths: list[SubmittedPath] = Field(min_length=1, max_length=4096)
    base_head: str | None = Field(default=None, max_length=128)
    run_trace_id: str | None = Field(default=None, max_length=128)
    trailers: SourceTrailers | None = None
    admission_record_digest: str | None = Field(default=None, max_length=128)


class SourceLandParams(Params):
    """Land one submitted changeset: fast gates, then the engine's policy."""

    repository: str = Field(min_length=1, max_length=_NAME_MAX)
    branch: str = Field(min_length=1, max_length=_NAME_MAX)
    changeset: str = Field(min_length=1, max_length=128)


class SourceRevertParams(SourceLandParams):
    """Revert one landed changeset through the engine's revert decision path."""


class SourceWriteResult(Params):
    value: dict[str, Any]


async def require_admission_record(
    context: Any, body: Mapping[str, Any], digest: str | None
) -> None:
    """An agent's changeset needs a committed, matching ``GeneratedCodeAdmission`` record.

    The verdict must be admit, the tenant the caller's and the artifact digest the digest
    of the proposed content; anything else refuses ``POLICY_DENIED`` with zero writes
    (ARBORA-OPS-R869). Arbora keeps the materialization authority.
    """
    from arbora.api.ops._decide import committed_record, record_digest

    if getattr(context.caller, "principal_kind", None) != "agent":
        return
    missing = OperationRefused("POLICY_DENIED", {"reason": "admission_record_missing"})
    if not digest:
        raise missing
    record = await committed_record(context, digest)
    if (
        record.get("kind") != "GeneratedCodeAdmission"
        or record.get("verdict") != "admit"
        or str(record.get("tenant")) != str(context.caller.tenant)
    ):
        raise missing
    if record.get("artifact_digest") != record_digest(body):
        raise OperationRefused(
            "POLICY_DENIED", {"reason": "admission_artifact_mismatch"}
        )


async def handle_source_commit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """``SubmitChangeset`` under the caller's claims; engine refusals pass through."""
    request = SourceCommitParams.model_validate(dict(params))
    body = request.model_dump(mode="json", exclude_none=True)
    body["repo"] = body.pop("repository")
    cited = body.pop("admission_record_digest", None)
    await require_admission_record(context, body, cited)
    answer = await engine_send(
        context, "SubmitChangeset", {"request": body}, idempotent=True
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_source_land(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    from arbora.api.ops.source_landing import land_changeset

    request = SourceLandParams.model_validate(dict(params))
    return {
        "value": await land_changeset(
            context,
            repository=request.repository,
            branch=request.branch,
            changeset=request.changeset,
        )
    }


async def handle_source_revert(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Revert is the engine's decision path (MEDULLA-SCM-R009).

    PIN-LAG: neither the pinned release nor engine ``main`` publishes a revert
    method yet, so the operation refuses ``UNAVAILABLE`` naming it.
    """
    from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

    SourceRevertParams.model_validate(dict(params))
    try:
        require_methods("RevertChangeset")
    except PinLagUnavailable as missing:
        raise refusal(missing) from None
    request = SourceRevertParams.model_validate(dict(params))
    answer = await engine_send(
        context,
        "RevertChangeset",
        {
            "repo": request.repository,
            "branch": request.branch,
            "changeset": request.changeset,
        },
        idempotent=True,
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


def _write_op(
    op_id: str, summary: str, example: str, params: type[Params], handler: str
) -> OpSpec:
    return OpSpec(
        id=op_id,
        verb=Verb.ACT,
        summary=summary,
        examples=(example,),
        params=params,
        result=SourceWriteResult,
        binding=Composite(handler=handler),
        scopes=frozenset({"blob:write", "graph:write"}),
        effect=Effect.DESTRUCTIVE,
        confirm=Confirm.PLAN,
        idempotency=Idempotency.KEY_REQUIRED,
        audit=AuditClass.IDENTITY_CHAIN,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _write_op(
            "source.commit",
            "Submit one changeset to one branch of one repository",
            "commit these file changes to the branch",
            SourceCommitParams,
            "arbora.api.ops.source.handle_source_commit",
        ),
        _write_op(
            "source.land",
            "Run the fast gates on a submitted changeset and record the verdicts",
            "land this changeset once the fast gates pass",
            SourceLandParams,
            "arbora.api.ops.source.handle_source_land",
        ),
        _write_op(
            "source.revert",
            "Revert one landed changeset through the engine decision path",
            "revert this landed changeset",
            SourceRevertParams,
            "arbora.api.ops.source.handle_source_revert",
        ),
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
            op_id="source.diff",
            summary="List the paths that differ between two refs of one repository",
            examples=("what changed between main and this branch",),
            params=SourceDiffParams,
            result=SourceDiffResult,
            handler="arbora.api.ops.source.handle_source_diff",
            scopes=frozenset({"node:read", "edge:read", "blob:read"}),
        ),
        _write_op(
            "source.edit_symbol",
            "Edit one symbol of one branch through the graph-native edit contract",
            "replace the body of this function",
            SymbolEditParams,
            "arbora.api.ops.source.handle_source_edit_symbol",
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
    "SourceCommitParams",
    "SourceDiffEntry",
    "SourceDiffOutcome",
    "SourceDiffParams",
    "SourceDiffResult",
    "SymbolEditOpParams",
    "SymbolEditParams",
    "SourceHistoryEntry",
    "SourceHistoryOutcome",
    "SourceHistoryParams",
    "SourceHistoryResult",
    "SourceLandParams",
    "SourceRevertParams",
    "SourceReader",
    "SourceTreeEntry",
    "SourceTreeOutcome",
    "SourceTreeParams",
    "SourceTreeResult",
    "handle_source_blob",
    "handle_source_blame",
    "handle_source_commit",
    "handle_source_diff",
    "handle_source_edit_symbol",
    "handle_source_land",
    "handle_source_revert",
    "handle_source_history",
    "handle_source_tree",
    "operations",
    "specs",
]
