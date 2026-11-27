"""Bounded bookmark search, relationships and export (ARBORA-OPS-R066).

Medulla owns the semantic index and the relations (MEDULLA-MEDIA-VOCAB-R001.102).
``bookmarks.search`` and ``bookmarks.relationships`` forward the engine
``BookmarkSemanticSearch`` and ``BookmarkRelations`` reads as the verified
caller; the engine pins one index revision and one graph snapshot per call and
binds both into the answer, so a search and the relations of its hits read
identical scope. ``bookmarks.export`` serializes one search page as CSV or JSON.
Arbora fetches no imported URL, executes no imported instruction and embeds
nothing: the query embedding comes from the caller.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.registry import OpSpec
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_methods

READ_SCOPE = "memory:read"
#: Characters that turn a CSV cell into a spreadsheet formula.
_FORMULA = ("=", "+", "-", "@", "\t", "\r")
EXPORT_FORMAT_REVISION = 1
_COLUMNS = ("id", "score", "source_id", "provenance_json", "metadata_json")


class _Limits(Params):
    """The engine limit policy; the caller may lower each bound, never exceed the engine's."""

    max_hits: int = Field(default=50, ge=1, le=200)
    max_relationships: int = Field(default=200, ge=1, le=1000)
    max_response_bytes: int = Field(default=262_144, ge=1024, le=1_048_576)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)

    def policy(self) -> dict[str, int]:
        return {
            "max_hits": self.max_hits,
            "max_relationships": self.max_relationships,
            "max_response_bytes": self.max_response_bytes,
        }


class BookmarkSearchParams(_Limits):
    """A semantic search over named bookmark sources by a caller-supplied embedding."""

    schema_version: Literal[1] = 1
    query_embedding: list[float] = Field(min_length=1, max_length=4096)
    source_ids: list[str] = Field(min_length=1, max_length=64)


class BookmarkRelationshipsParams(_Limits):
    """The authorized relations leaving the named bookmark nodes."""

    schema_version: Literal[1] = 1
    node_ids: list[str] = Field(min_length=1, max_length=200)


class BookmarkExportParams(BookmarkSearchParams):
    """One search page serialized as ``csv`` or ``json``."""

    format: Literal["csv", "json"] = "json"
    max_hits: int = Field(default=50, ge=1, le=1_000_000)


class BookmarkResult(Params):
    value: dict[str, Any]


def schema_digest(model: type[Params]) -> str:
    """SHA-256 of the model's canonical JSON schema (lowercase hex)."""
    canonical = json.dumps(
        model.model_json_schema(), sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _read(context: Any, method: str, request: dict[str, Any]) -> dict[str, Any]:
    try:
        require_methods(method)
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    answer = await engine_send(context, method, {"request": request})
    if not isinstance(answer, Mapping) or "binding" not in answer:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    return dict(answer)


def _search_request(params: Mapping[str, Any]) -> dict[str, Any]:
    request = {
        "query_embedding": params["query_embedding"],
        "source_ids": params["source_ids"],
        "limit_policy": {
            key: params[key]
            for key in ("max_hits", "max_relationships", "max_response_bytes")
        },
    }
    if params.get("cursor") is not None:
        request["cursor"] = params["cursor"]
    return request


async def handle_bookmarks_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Search the caller's bookmarks; the engine answer returns unchanged."""
    return {
        "value": await _read(context, "BookmarkSemanticSearch", _search_request(params))
    }


async def handle_bookmarks_relationships(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The authorized relations of the named nodes; the engine answer returns unchanged."""
    request = {
        "node_ids": params["node_ids"],
        "limit_policy": {
            key: params[key]
            for key in ("max_hits", "max_relationships", "max_response_bytes")
        },
    }
    if params.get("cursor") is not None:
        request["cursor"] = params["cursor"]
    return {"value": await _read(context, "BookmarkRelations", request)}


def _cell(value: Any) -> str:
    """One CSV cell; a leading formula character gets a single quote."""
    text = str(value)
    return "'" + text if text.startswith(_FORMULA) else text


def csv_export(hits: list[Mapping[str, Any]]) -> str:
    """The hits as CSV; provenance and unknown keys ride in JSON columns, never dropped."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(_COLUMNS)
    for hit in hits:
        writer.writerow(
            [
                _cell(hit.get("id", "")),
                _cell(hit.get("score", "")),
                _cell(hit.get("source_id", "")),
                _cell(json.dumps(hit.get("provenance", {}), sort_keys=True)),
                _cell(
                    json.dumps(
                        {
                            "unknown_keys": hit.get("unknown_keys", []),
                            "cursor": hit.get("cursor"),
                        },
                        sort_keys=True,
                    )
                ),
            ]
        )
    return out.getvalue()


async def handle_bookmarks_export(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read one search page and serialize it; a page above the row cap is refused."""
    from arbora.core.settings import runtime_config

    cap = int(runtime_config.bookmarks_max_export_rows)
    if params["max_hits"] > cap:
        raise OperationRefused("LOAD_CAP_EXCEEDED", {"limit": cap})
    answer = await _read(context, "BookmarkSemanticSearch", _search_request(params))
    hits = [dict(hit) for hit in answer.get("hits", [])]
    if len(hits) > cap:
        raise OperationRefused("LOAD_CAP_EXCEEDED", {"limit": cap})
    body: Any = csv_export(hits) if params["format"] == "csv" else hits
    return {
        "value": {
            "format": params["format"],
            "format_revision": EXPORT_FORMAT_REVISION,
            "rows": len(hits),
            "binding": answer["binding"],
            "content": body,
        }
    }


def operations() -> tuple[OpSpec, ...]:
    def read(
        op_id: str, summary: str, example: str, params: type[Params], handler: str
    ) -> OpSpec:
        return build_read_op(
            op_id=op_id,
            summary=summary,
            examples=(example,),
            params=params,
            result=BookmarkResult,
            handler=f"arbora.api.ops.bookmarks.{handler}",
            scope=READ_SCOPE,
        )

    return (
        read(
            "bookmarks.search",
            "Search the caller's bookmarks by semantic similarity",
            "find bookmarks like this embedding",
            BookmarkSearchParams,
            "handle_bookmarks_search",
        ),
        read(
            "bookmarks.relationships",
            "List the authorized relations leaving bookmark nodes",
            "show how these bookmarks relate",
            BookmarkRelationshipsParams,
            "handle_bookmarks_relationships",
        ),
        read(
            "bookmarks.export",
            "Export one page of bookmark search hits as CSV or JSON",
            "download these bookmarks as CSV",
            BookmarkExportParams,
            "handle_bookmarks_export",
        ),
    )


specs = operations

__all__ = [
    "BookmarkExportParams",
    "BookmarkRelationshipsParams",
    "BookmarkResult",
    "BookmarkSearchParams",
    "csv_export",
    "handle_bookmarks_export",
    "handle_bookmarks_relationships",
    "handle_bookmarks_search",
    "operations",
    "schema_digest",
    "specs",
]
