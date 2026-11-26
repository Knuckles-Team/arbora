"""Catalog browse, search and detail served over HTTP and MCP (ARBORA-OPS-R921)."""

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError

from arbora.api.invoke.pipeline import OperationRefused  # noqa: F401
from tests.api._served_support import (
    McpAsk,
    capture_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

ASSETS: list[dict[str, Any]] = [
    {
        "asset": "dashboard:a",
        "kind": "DashboardAsset",
        "name": "Sales",
        "owner": "person:one",
    },
    {
        "asset": "dashboard:b",
        "kind": "DashboardAsset",
        "name": "Costs",
        "owner": "person:two",
    },
    {"asset": "glossary:margin", "kind": "Article", "name": "Margin", "owner": None},
    {
        "asset": "secret:hidden",
        "kind": "Article",
        "name": "Hidden",
        "owner": "person:two",
        "hidden": True,
    },
]


def _catalog(params: dict[str, Any], _principal: str) -> Any:
    op = params["op"]
    visible = [a for a in ASSETS if not a.get("hidden")]
    action = op["op"]
    if action == "browse":
        rows = [a for a in visible if op.get("kind") in (None, a["kind"])]
        start = int(op.get("cursor") or 0)
        page = rows[start : start + op["limit"]]
        counts: dict[str, int] = {}
        for a in visible:
            counts[a["kind"]] = counts.get(a["kind"], 0) + 1
        return {
            "kind_counts": [{"kind": k, "count": n} for k, n in counts.items()],
            "rows": [
                {k: v for k, v in a.items() if k != "hidden" and v is not None}
                for a in page
            ],
            "next_cursor": str(start + op["limit"])
            if start + op["limit"] < len(rows)
            else None,
        }
    if action == "search":
        if len(op["text"]) > 1000:
            raise EngineResponseError("CATALOG_QUERY_TOO_LONG", "too long")
        hits = [a for a in visible if op["text"].lower() in a["name"].lower()]
        return {
            "kind_counts": [],
            "rows": [
                {
                    "asset": a["asset"],
                    "kind": a["kind"],
                    "name": a["name"],
                    "score": 2.5,
                }
                for a in hits
            ],
        }
    found = next((a for a in visible if a["asset"] == op["asset_id"]), None)
    if found is None:
        raise EngineResponseError("ASSET_NOT_FOUND", "no such asset")
    return {
        "kind_counts": [],
        "rows": [],
        "detail": {
            "asset": found["asset"],
            "kind": found["kind"],
            "name": found["name"],
            "owners": ["person:one"],
            "terms": ["term:margin"],
            "upstream": ["dataset:x"],
            "downstream": [],
            "rating_count": 2,
            "rating_mean": 4.5,
            "comments": [
                {
                    "id": "c1",
                    "text": "good",
                    "author": "person:two",
                    "at_ms": 1_700_000_000_000,
                }
            ],
        },
    }


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> Any:
    invocation = capture_invocation(monkeypatch)
    return invocation


@pytest.fixture
def app(catalog: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    route_methods(
        client,
        {"CatalogRead": _catalog},
        monkeypatch,
        {"fixture-verified": person("one")},
    )
    return app, catalog["invocation"], monkeypatch


@pytest.mark.spec("ARBORA-OPS-R921.1")
def test_browse_counts_kinds_and_hides_unreadable_assets(app: Any) -> None:
    http, _invocation, _mp = app
    first = _value(_call(http, "catalog.assets.browse", {"limit": 2}))
    assert first["facets"] == [
        {"kind": "DashboardAsset", "count": 2},
        {"kind": "Article", "count": 1},
    ]
    assert [i["asset_id"] for i in first["items"]] == ["dashboard:a", "dashboard:b"]
    assert first["items"][0] == {
        "asset_id": "dashboard:a",
        "kind": "DashboardAsset",
        "name": "Sales",
        "owner": "person:one",
    }
    rest = _value(
        _call(
            http, "catalog.assets.browse", {"limit": 2, "cursor": first["next_cursor"]}
        )
    )
    assert [i["asset_id"] for i in rest["items"]] == ["glossary:margin"]
    assert "secret:hidden" not in str(first) + str(rest)
    only = _value(_call(http, "catalog.assets.browse", {"kind": "Article"}))
    assert [i["asset_id"] for i in only["items"]] == ["glossary:margin"]
    assert _call(http, "catalog.assets.browse", {"limit": 500}).status_code == 400


@pytest.mark.spec("ARBORA-OPS-R921.2")
def test_search_passes_engine_ranking_and_too_long_refusal(app: Any) -> None:
    http, _invocation, _mp = app
    found = _value(_call(http, "catalog.assets.search", {"text": "sales"}))
    assert found["items"] == [
        {
            "asset_id": "dashboard:a",
            "kind": "DashboardAsset",
            "name": "Sales",
            "score": 2.5,
        }
    ]
    assert _call(http, "catalog.assets.search", {"text": ""}).status_code == 400
    too_long = _call(http, "catalog.assets.search", {"text": "x" * 1000})
    assert too_long.status_code == 200
    over = _call(http, "catalog.assets.search", {"text": "x" * 1001})
    assert over.status_code == 400


@pytest.mark.spec("ARBORA-OPS-R921.3")
def test_get_returns_detail_and_refuses_unreadable_with_asset_not_found(
    app: Any,
) -> None:
    http, _invocation, _mp = app
    detail = _value(_call(http, "catalog.assets.get", {"asset_id": "dashboard:a"}))
    assert detail["lineage"] == {"upstream": ["dataset:x"], "downstream": []}
    assert detail["ratings"] == {"count": 2, "mean": 4.5}
    assert detail["comments"] == [
        {
            "id": "c1",
            "author": "person:two",
            "at": "2023-11-14T22:13:20.000Z",
            "text": "good",
        }
    ]
    for unreadable in ("secret:hidden", "nothing:here"):
        refused = _call(http, "catalog.assets.get", {"asset_id": unreadable})
        assert refused.status_code == 404
        assert refused.json()["error"]["code"] == "ASSET_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R921", "ARBORA-OPS-R921.4")
def test_catalog_journey_over_mcp(app: Any) -> None:
    _http, invocation, monkeypatch = app
    ask = McpAsk(invocation, monkeypatch)
    browsed = ask("ask", "catalog.assets.browse", {"limit": 50})
    assert browsed["ok"] is True, browsed
    hit = ask("ask", "catalog.assets.search", {"text": "margin"})["result"]["items"][0]
    opened = ask("ask", "catalog.assets.get", {"asset_id": hit["asset_id"]})
    assert opened["result"]["terms"] == ["term:margin"]
    fleet_calls = [
        c for c in invocation.services.registry if c.id == "fleet.catalog.search"
    ]
    assert fleet_calls and ask("ask", "catalog.assets.browse", {})["ok"] is True
