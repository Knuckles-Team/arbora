"""Harness session browse, search and cost reads (ARBORA-OPS-R060.3).

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

from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec

SESSIONS_SCOPE = "sessions:read"
STATEMENT = "harness_session_search"
_CURSOR_PREFIX = "next_cursor: "
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
    answer = await engine_send(context, "Uql", _query(params))
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
    )


specs = operations

__all__ = [
    "handle_sessions_browse",
    "handle_sessions_costs",
    "handle_sessions_search",
    "operations",
    "specs",
]
