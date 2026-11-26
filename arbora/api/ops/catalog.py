"""Catalog browse, search and detail reads (ARBORA-OPS-R921).

``catalog.assets.browse``, ``catalog.assets.search`` and ``catalog.assets.get``
forward the engine ``CatalogRead`` actions ``browse``, ``search`` and
``detail``. Arbora holds no asset, no index and no ranking: the engine narrows
every answer to what the caller may read and scores every hit. These reads are
distinct from ``fleet.catalog.search``, which lists fleet tools.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op
from arbora.api.ops._engine import engine_send, rfc3339, unavailable
from arbora.api.registry import OpSpec

CATALOG_READ_SCOPE = "kg:read"


class CatalogBrowseParams(Params):
    kind: str | None = Field(default=None, min_length=1, max_length=64)
    owner: str | None = Field(default=None, min_length=1, max_length=256)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class CatalogSearchParams(Params):
    text: str = Field(min_length=1, max_length=1000)
    kinds: list[str] = Field(default_factory=list, max_length=32)
    limit: int = Field(default=25, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class CatalogAssetParams(Params):
    asset_id: str = Field(min_length=1, max_length=512)


class CatalogFacet(Params):
    kind: str
    count: int


class CatalogItem(Params):
    asset_id: str
    kind: str
    name: str | None = None
    owner: str | None = None
    score: float | None = None


class CatalogBrowseResult(Params):
    facets: list[CatalogFacet]
    items: list[CatalogItem]
    next_cursor: str | None = None


class CatalogSearchResult(Params):
    items: list[CatalogItem]
    next_cursor: str | None = None


class CatalogLineage(Params):
    upstream: list[str]
    downstream: list[str]


class CatalogRatings(Params):
    count: int
    mean: float | None = None


class CatalogComment(Params):
    id: str
    author: str | None = None
    at: str | None = None
    text: str


class CatalogAssetResult(Params):
    asset_id: str
    kind: str
    name: str | None = None
    owners: list[str]
    terms: list[str]
    lineage: CatalogLineage
    ratings: CatalogRatings
    comments: list[CatalogComment]


async def _read(context: Any, action: dict[str, Any]) -> Mapping[str, Any]:
    """One ``CatalogRead`` for the verified caller; the engine narrows the answer."""
    result = await engine_send(context, "CatalogRead", {"op": action})
    if not isinstance(result, Mapping):
        raise unavailable("engine result malformed")
    return result


def _item(row: Any) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise unavailable("engine result malformed")
    try:
        item = {"asset_id": row["asset"], "kind": row["kind"]}
    except KeyError:
        raise unavailable("engine result malformed") from None
    for key in ("name", "owner", "score"):
        if row.get(key) is not None:
            item[key] = row[key]
    return item


def _items(answer: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = answer.get("rows")
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    return [_item(row) for row in rows]


def _paged(action: dict[str, Any], params: Mapping[str, Any]) -> dict[str, Any]:
    action["limit"] = params["limit"]
    if params.get("cursor") is not None:
        action["cursor"] = params["cursor"]
    return action


async def handle_catalog_browse(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Kind counts and one page of assets, as the engine counts and pages them."""
    action: dict[str, Any] = {"op": "browse"}
    for key in ("kind", "owner"):
        if params.get(key) is not None:
            action[key] = params[key]
    answer = await _read(context, _paged(action, params))
    counts = answer.get("kind_counts")
    if not isinstance(counts, list):
        raise unavailable("engine result malformed")
    try:
        facets = [{"kind": c["kind"], "count": c["count"]} for c in counts]
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None
    return {
        "facets": facets,
        "items": _items(answer),
        "next_cursor": answer.get("next_cursor"),
    }


async def handle_catalog_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Ranked text search; the score is the engine's and nothing is re-ranked."""
    if not params["text"].strip():
        raise OperationRefused("INVALID_ARGUMENT", {"field": "text"})
    action: dict[str, Any] = {"op": "search", "text": params["text"]}
    if params.get("kinds"):
        action["kinds"] = list(params["kinds"])
    answer = await _read(context, _paged(action, params))
    return {"items": _items(answer), "next_cursor": answer.get("next_cursor")}


def _comment(row: Any) -> dict[str, Any]:
    try:
        at = row.get("at_ms")
        return {
            "id": row["id"],
            "author": row.get("author"),
            "at": rfc3339(int(at)) if at is not None else None,
            "text": row["text"],
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        raise unavailable("engine result malformed") from None


async def handle_catalog_asset_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One asset's owners, terms, direct lineage neighbours, ratings and comments."""
    answer = await _read(context, {"op": "detail", "asset_id": params["asset_id"]})
    detail = answer.get("detail")
    if not isinstance(detail, Mapping):
        raise unavailable("engine result malformed")
    try:
        return {
            "asset_id": detail["asset"],
            "kind": detail["kind"],
            "name": detail.get("name"),
            "owners": list(detail["owners"]),
            "terms": list(detail["terms"]),
            "lineage": {
                "upstream": list(detail["upstream"]),
                "downstream": list(detail["downstream"]),
            },
            "ratings": {
                "count": detail["rating_count"],
                "mean": detail.get("rating_mean"),
            },
            "comments": [_comment(row) for row in detail["comments"]],
        }
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="catalog.assets.browse",
            summary="Browse catalog assets by kind with kind counts",
            examples=("browse the catalog by asset kind",),
            params=CatalogBrowseParams,
            result=CatalogBrowseResult,
            handler="arbora.api.ops.catalog.handle_catalog_browse",
            scope=CATALOG_READ_SCOPE,
        ),
        build_read_op(
            op_id="catalog.assets.search",
            summary="Search catalog assets with the engine's ranked index",
            examples=("search the catalog for a term",),
            params=CatalogSearchParams,
            result=CatalogSearchResult,
            handler="arbora.api.ops.catalog.handle_catalog_search",
            scope=CATALOG_READ_SCOPE,
        ),
        build_read_op(
            op_id="catalog.assets.get",
            summary="Read one catalog asset with owners, terms, lineage and ratings",
            examples=("open this catalog asset",),
            params=CatalogAssetParams,
            result=CatalogAssetResult,
            handler="arbora.api.ops.catalog.handle_catalog_asset_get",
            scope=CATALOG_READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "CatalogAssetParams",
    "CatalogBrowseParams",
    "CatalogSearchParams",
    "handle_catalog_asset_get",
    "handle_catalog_browse",
    "handle_catalog_search",
    "operations",
    "specs",
]
