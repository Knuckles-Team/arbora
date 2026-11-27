"""Harness session browse, search and cost reads, and the harness manifest
(ARBORA-OPS-R060.1 to R060.4).

The three operations read the engine statement ``harness_session_search``
through ``Uql`` as the verified caller. The first page begins with one summary
row (manifest counts, watermark and cost totals); each later row is one
session. A session the engine has not indexed stays in the counts as pending
or unavailable, and a cost the engine cannot measure stays ``unknown`` with its
reason. Arbora keeps no session index and no cost table.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, bound_service, build_read_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import OpSpec

SESSIONS_SCOPE = "sessions:read"
STATEMENT = "harness_session_search"
_CURSOR_PREFIX = "next_cursor: "
#: The engine refusal for a cursor minted at another index revision or filter.
_STALE_CURSOR = "SESSION_CURSOR_STALE"
HARNESS_STREAM_PREFIX = "harness-sessions/"
_SCHEMA_VERSION = 1
_Page = Field(default=25, ge=1, le=100)


class SessionsBrowseParams(Params):
    harness_id: str | None = Field(default=None, min_length=1, max_length=128)
    from_ms: int | None = Field(default=None, ge=0)
    to_ms: int | None = Field(default=None, ge=0)
    page_size: int = _Page
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class SessionsSearchParams(SessionsBrowseParams):
    text: str = Field(min_length=1, max_length=256)


class SessionsCostsParams(Params):
    harness_id: str | None = Field(default=None, min_length=1, max_length=128)
    from_ms: int | None = Field(default=None, ge=0)
    to_ms: int | None = Field(default=None, ge=0)


class SessionSummary(Params):
    index_revision: int = Field(ge=0)
    declared: int = Field(ge=0)
    received: int = Field(ge=0)
    indexed: int = Field(ge=0)
    unavailable: int = Field(ge=0)
    pending: int = Field(ge=0)
    watermark: str


class SessionEntry(Params):
    harness_id: str
    source_session_id: str
    status: str
    reason: str | None = None
    run_trace_id: str | None = None
    end_ms: int | None = None
    source_sequence: int = Field(ge=0)


class SessionsPage(Params):
    summary: SessionSummary | None = None
    sessions: list[SessionEntry]
    next_cursor: str | None = None
    partial: bool


class SessionCost(Params):
    status: Literal["measured", "unknown"]
    reason: str | None = None
    amount_minor: int | None = Field(default=None, ge=0)
    currency: str | None = None
    unit: Literal["minor"] = "minor"
    accounting_revision: str | None = None
    uncached_input: int | None = Field(default=None, ge=0)
    cached_input: int | None = Field(default=None, ge=0)
    output: int | None = Field(default=None, ge=0)
    reasoning_output: int | None = Field(default=None, ge=0)
    coverage: SessionSummary


def _query(params: Mapping[str, Any]) -> dict[str, Any]:
    """The statement's parameters, typed as the engine's UQL parameter union."""
    wire: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        wire[key] = {"Str": value} if isinstance(value, str) else {"Num": float(value)}
    return {"text": STATEMENT, "params": wire}


def _columns(row: Any) -> dict[str, str]:
    provenance = row.get("provenance") if isinstance(row, Mapping) else None
    if not isinstance(provenance, Mapping):
        raise unavailable("engine result malformed")
    return {str(name): str(value) for name, value in provenance.items()}


async def _read(
    context: Any, params: Mapping[str, Any]
) -> tuple[list[dict[str, str]], str | None]:
    try:
        answer = await engine_send(context, "Uql", _query(params))
    except EngineRefusal as refusal:
        if refusal.code == _STALE_CURSOR:
            # The cursor did not advance; the caller restarts from the first page.
            raise OperationRefused("STALE_CURSOR") from refusal
        raise
    body = answer.get("Rows") if isinstance(answer, Mapping) else None
    rows = body.get("rows") if isinstance(body, Mapping) else None
    if not isinstance(body, Mapping) or not isinstance(rows, list):
        raise unavailable("engine result malformed")
    cursor = next(
        (
            warning.removeprefix(_CURSOR_PREFIX)
            for warning in body.get("warnings", ())
            if isinstance(warning, str) and warning.startswith(_CURSOR_PREFIX)
        ),
        None,
    )
    return [_columns(row) for row in rows], cursor


def _whole(columns: Mapping[str, str], key: str) -> int | None:
    value = columns.get(key, "")
    return int(value) if value.isdigit() else None


def _summary(columns: Mapping[str, str]) -> dict[str, Any]:
    try:
        return {
            "index_revision": int(columns["index_revision"]),
            "declared": int(columns["declared"]),
            "received": int(columns["received"]),
            "indexed": int(columns["indexed"]),
            "unavailable": int(columns["unavailable"]),
            "pending": int(columns["pending"]),
            "watermark": columns.get("watermark", ""),
        }
    except (KeyError, ValueError):
        raise unavailable("engine result malformed") from None


def _entry(columns: Mapping[str, str]) -> dict[str, Any]:
    try:
        return {
            "harness_id": columns["harness_id"],
            "source_session_id": columns["source_session_id"],
            "status": columns["status"],
            "reason": columns.get("reason") or None,
            "run_trace_id": columns.get("run_trace_id") or None,
            "end_ms": _whole(columns, "end_ms"),
            "source_sequence": int(columns["source_sequence"]),
        }
    except (KeyError, ValueError):
        raise unavailable("engine result malformed") from None


async def _page(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    rows, cursor = await _read(context, params)
    summary = next((r for r in rows if r.get("kind") == "summary"), None)
    entries = [_entry(r) for r in rows if r.get("kind") == "entry"]
    shaped = _summary(summary) if summary is not None else None
    # A page after the first has no summary row and cannot tell what is missing.
    partial = bool(shaped and (shaped["unavailable"] or shaped["pending"]))
    return {
        "summary": shaped,
        "sessions": entries,
        "next_cursor": cursor,
        "partial": partial,
    }


async def handle_sessions_browse(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One page of sessions, newest source sequence first."""
    return await _page(context, params)


async def handle_sessions_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One page of sessions whose harness or source id contains the text."""
    return await _page(context, params)


async def handle_sessions_costs(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The engine's cost total over the matching indexed sessions."""
    rows, _ = await _read(context, {**params, "page_size": 1})
    summary = next((r for r in rows if r.get("kind") == "summary"), None)
    if summary is None:
        raise unavailable("engine result malformed")
    measured = summary.get("cost_status") == "measured"
    cost: dict[str, Any] = {
        "status": "measured" if measured else "unknown",
        "reason": None if measured else summary.get("cost_reason") or "unknown",
        "coverage": _summary(summary),
    }
    if measured:
        cost.update(
            amount_minor=_whole(summary, "cost_minor"),
            currency=summary.get("currency") or None,
            accounting_revision=summary.get("accounting_revision") or None,
            **{key: _whole(summary, key) for key in _TOKENS},
        )
    return SessionCost.model_validate(cost).model_dump(mode="json")


_TOKENS = ("uncached_input", "cached_input", "output", "reasoning_output")


class HarnessesManifestParams(Params):
    pass


class HarnessEntry(Params):
    harness_id: str
    server: str
    sync_state: str
    records: int | None = Field(default=None, ge=0)
    last_job_id: str | None = None


class HarnessManifest(Params):
    schema_version: int = Field(default=_SCHEMA_VERSION, ge=1)
    harnesses: list[HarnessEntry]


class DiscoveryStatusParams(Params):
    harness_id: str | None = Field(default=None, min_length=1, max_length=128)


class DiscoveryProgress(Params):
    schema_version: int = Field(default=_SCHEMA_VERSION, ge=1)
    harness_id: str
    state: Literal["not_started", "queued", "running", "completed", "failed"]
    reason: str | None = None
    indexed_session_revision: int | None = Field(default=None, ge=0)


class DiscoveryStatus(Params):
    discovery: list[DiscoveryProgress]


async def _declared_harnesses(context: Any) -> list[HarnessEntry]:
    """The harness streams the connector sync declares, read as the caller.

    A harness is a declared connector source named ``harness-sessions/<id>``
    (SCION-SOURCE-INGEST-R013.2). Arbora lists no harness of its own.
    """
    runner = bound_service(
        context, "ingest_runner", reason="the connector sync is not composed"
    )
    inventory = await runner.list_sources(
        tenant=context.caller.tenant, caller=context.caller
    )
    entries: list[HarnessEntry] = []
    for record in inventory.sources:
        server, _, source = record.source_id.partition("/")
        if source.startswith(HARNESS_STREAM_PREFIX) and source != HARNESS_STREAM_PREFIX:
            entries.append(
                HarnessEntry(
                    harness_id=source.removeprefix(HARNESS_STREAM_PREFIX),
                    server=server,
                    sync_state=record.sync_state or "not_started",
                    records=record.records,
                    last_job_id=record.last_job_id,
                )
            )
    return sorted(entries, key=lambda entry: (entry.harness_id, entry.server))


async def handle_harness_manifest(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Every harness the connector sync declares, with its sync state."""
    manifest = HarnessManifest(harnesses=await _declared_harnesses(context))
    return manifest.model_dump(mode="json")


async def _job_state(context: Any, entry: HarnessEntry) -> tuple[str, str | None]:
    if entry.last_job_id is None:
        return "not_started", None
    runner = bound_service(
        context, "ingest_runner", reason="the connector sync is not composed"
    )
    try:
        job = await runner.get_job_status(
            tenant=context.caller.tenant, job_id=entry.last_job_id
        )
    except OperationRefused as refusal:
        if refusal.code != "NOT_FOUND":
            raise
        return "not_started", None
    return job.status, job.reason


async def handle_discovery_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Per harness: the sync job's state and the engine's indexed revision."""
    entries = await _declared_harnesses(context)
    wanted = params.get("harness_id")
    if wanted is not None:
        entries = [entry for entry in entries if entry.harness_id == wanted]
        if not entries:
            raise OperationRefused("INVALID_ARGUMENT", {"field": "harness_id"})
    progress: list[DiscoveryProgress] = []
    for entry in entries:
        state, reason = await _job_state(context, entry)
        rows, _ = await _read(context, {"harness_id": entry.harness_id, "page_size": 1})
        summary = next((r for r in rows if r.get("kind") == "summary"), None)
        progress.append(
            DiscoveryProgress(
                harness_id=entry.harness_id,
                state=state,  # type: ignore[arg-type]
                reason=reason,
                indexed_session_revision=(
                    _summary(summary)["index_revision"] if summary else None
                ),
            )
        )
    return DiscoveryStatus(discovery=progress).model_dump(mode="json")


def operations() -> tuple[OpSpec, ...]:
    handlers = "arbora.api.ops.sessions.handle_sessions_"
    return (
        build_read_op(
            op_id="sessions.browse",
            summary="Browse the caller's indexed harness sessions, newest first",
            examples=("list my recent harness sessions",),
            params=SessionsBrowseParams,
            result=SessionsPage,
            handler=handlers + "browse",
            scope=SESSIONS_SCOPE,
        ),
        build_read_op(
            op_id="sessions.search",
            summary="Search the caller's harness sessions by harness or session id",
            examples=("find harness sessions matching this text",),
            params=SessionsSearchParams,
            result=SessionsPage,
            handler=handlers + "search",
            scope=SESSIONS_SCOPE,
        ),
        build_read_op(
            op_id="sessions.costs",
            summary="Read the measured cost of the caller's indexed harness sessions",
            examples=("what did my harness sessions cost",),
            params=SessionsCostsParams,
            result=SessionCost,
            handler=handlers + "costs",
            scope=SESSIONS_SCOPE,
        ),
        build_read_op(
            op_id="harnesses.manifest",
            summary="List the harnesses the connector sync declares",
            examples=("which harnesses are synced",),
            params=HarnessesManifestParams,
            result=HarnessManifest,
            handler="arbora.api.ops.sessions.handle_harness_manifest",
            scope=SESSIONS_SCOPE,
        ),
        build_read_op(
            op_id="harnesses.discovery.status",
            summary="Read each harness's sync job state and indexed session revision",
            examples=("how far has harness discovery got",),
            params=DiscoveryStatusParams,
            result=DiscoveryStatus,
            handler="arbora.api.ops.sessions.handle_discovery_status",
            scope=SESSIONS_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_discovery_status",
    "handle_harness_manifest",
    "handle_sessions_browse",
    "handle_sessions_costs",
    "handle_sessions_search",
    "operations",
    "specs",
]
