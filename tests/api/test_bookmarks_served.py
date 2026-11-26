"""Bounded bookmark search, relationships and export (ARBORA-OPS-R066)."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import bookmarks
from arbora.api.ops.registry_factory import get_registry
from arbora.pin_lag import medulla as pin_lag
from tests.api._lane_double import EngineDouble, context

OPS = {op.id: op for op in bookmarks.operations()}
SEARCH = {
    "query_embedding": [0.1, 0.2],
    "source_ids": ["import:1"],
    "max_hits": 50,
    "max_relationships": 200,
    "max_response_bytes": 262_144,
}
BINDING = {
    "index_revision": 7,
    "graph_snapshot_revision": 9,
    "source_ids": ["import:1"],
}
HITS = [
    {
        "id": "bm:1",
        "score": 0.9,
        "source_id": "import:1",
        "provenance": {"importer": "offline"},
        "unknown_keys": ["vendor_x"],
        "cursor": None,
    },
    {
        "id": "=HYPERLINK(1)",
        "score": 0.5,
        "source_id": "import:1",
        "provenance": {},
        "unknown_keys": [],
        "cursor": None,
    },
]
EDGES = [
    {
        "source_node_id": "bm:1",
        "target_node_id": "bm:2",
        "relationship": "relatedTo",
        "provenance": None,
        "cursor": None,
    }
]


def _engine_serves(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(bookmarks, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS)
        | {"BookmarkSemanticSearch", "BookmarkRelations"},
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


def _double() -> EngineDouble:
    return EngineDouble(
        BookmarkSemanticSearch={"hits": HITS, "binding": BINDING},
        BookmarkRelations={"edges": EDGES, "binding": BINDING},
    )


@pytest.mark.spec("ARBORA-OPS-R066.1")
def test_models_round_trip_and_schema_digests_are_stable() -> None:
    parsed = bookmarks.BookmarkSearchParams(**SEARCH)
    assert parsed.model_dump(exclude_none=True)["source_ids"] == ["import:1"]
    for bad in (
        {**SEARCH, "max_hits": 0},
        {**SEARCH, "max_hits": 201},
        {**SEARCH, "query_embedding": []},
        {**SEARCH, "source_ids": []},
        {**SEARCH, "fetch_url": "http://x"},
    ):
        with pytest.raises(ValidationError):
            bookmarks.BookmarkSearchParams(**bad)
    digest = bookmarks.schema_digest(bookmarks.BookmarkSearchParams)
    assert len(digest) == 64 and digest == bookmarks.schema_digest(
        bookmarks.BookmarkSearchParams
    )
    assert digest != bookmarks.schema_digest(bookmarks.BookmarkRelationshipsParams)


@pytest.mark.spec("ARBORA-OPS-R066.2")
@pytest.mark.asyncio
async def test_search_and_relationships_share_one_snapshot_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = _double()
    ctx = context(client, monkeypatch)
    found = await bookmarks.handle_bookmarks_search(
        ctx, SEARCH, OPS["bookmarks.search"]
    )
    related = await bookmarks.handle_bookmarks_relationships(
        ctx,
        {
            "node_ids": ["bm:1"],
            "max_hits": 50,
            "max_relationships": 200,
            "max_response_bytes": 262_144,
        },
        OPS["bookmarks.relationships"],
    )
    assert found["value"] == {"hits": HITS, "binding": BINDING}  # unchanged
    assert related["value"]["binding"] == found["value"]["binding"]
    assert [call[0] for call in client.sent] == [
        "BookmarkSemanticSearch",
        "BookmarkRelations",
    ]
    assert client.claims == [{"principal": "alice"}] * 2
    assert all(op.scopes == {"memory:read"} for op in OPS.values())
    assert set(OPS) <= {op.id for op in get_registry()}


@pytest.mark.spec("ARBORA-OPS-R066.3")
@pytest.mark.asyncio
async def test_csv_escapes_formula_cells_and_json_preserves_unknown_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    ctx = context(_double(), monkeypatch)
    exported = await bookmarks.handle_bookmarks_export(
        ctx, {**SEARCH, "format": "csv"}, OPS["bookmarks.export"]
    )
    rows = list(csv.reader(io.StringIO(exported["value"]["content"])))
    assert rows[0] == list(bookmarks._COLUMNS) and rows[2][0] == "'=HYPERLINK(1)"
    assert "vendor_x" in rows[1][4]  # unknown metadata rides in metadata_json
    assert exported["value"]["format_revision"] == bookmarks.EXPORT_FORMAT_REVISION
    as_json = await bookmarks.handle_bookmarks_export(
        ctx, {**SEARCH, "format": "json"}, OPS["bookmarks.export"]
    )
    assert as_json["value"]["content"] == HITS and as_json["value"]["rows"] == 2
    assert json.dumps(as_json["value"]["content"][0]["unknown_keys"]) == '["vendor_x"]'


@pytest.mark.spec("ARBORA-OPS-R066.4")
@pytest.mark.asyncio
async def test_refusals_return_before_any_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from medulla import EngineResponseError

    from arbora.core.settings import runtime_config

    # A pin without the methods: UNAVAILABLE naming one, zero engine calls.
    client = _double()
    with pytest.raises(OperationRefused) as refused:
        await bookmarks.handle_bookmarks_search(
            context(client, monkeypatch), SEARCH, OPS["bookmarks.search"]
        )
    assert refused.value.code == "UNAVAILABLE" and client.sent == []
    _engine_serves(monkeypatch)
    # An export above the row cap is refused before the engine reads anything.
    monkeypatch.setattr(runtime_config, "bookmarks_max_export_rows", 100)
    over = _double()
    with pytest.raises(OperationRefused) as capped:
        await bookmarks.handle_bookmarks_export(
            context(over, monkeypatch),
            {**SEARCH, "max_hits": 101, "format": "json"},
            OPS["bookmarks.export"],
        )
    assert capped.value.code == "LOAD_CAP_EXCEEDED" and over.sent == []
    # A stale index revision keeps the engine's own code and returns no hits.
    for code in ("BOOKMARK_INDEX_STALE", "BOOKMARK_REVISION_MISMATCH"):
        stale = EngineDouble(BookmarkSemanticSearch=EngineResponseError(code, "stale"))
        with pytest.raises(EngineRefusal) as engine_refused:
            await bookmarks.handle_bookmarks_search(
                context(stale, monkeypatch), SEARCH, OPS["bookmarks.search"]
            )
        assert engine_refused.value.code == code


@pytest.mark.spec("ARBORA-OPS-R066", "ARBORA-OPS-R066.5")
@pytest.mark.asyncio
async def test_ingest_to_browser_search_relationships_and_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _engine_serves(monkeypatch)
    client = _double()
    ctx = context(client, monkeypatch)
    found = await bookmarks.handle_bookmarks_search(
        ctx, SEARCH, OPS["bookmarks.search"]
    )
    ids = [hit["id"] for hit in found["value"]["hits"]]
    exported = await bookmarks.handle_bookmarks_export(
        ctx, {**SEARCH, "format": "json"}, OPS["bookmarks.export"]
    )
    # The download matches the engine snapshot it was read at.
    assert [hit["id"] for hit in exported["value"]["content"]] == ids
    assert exported["value"]["binding"] == found["value"]["binding"]
