"""SPARQL 1.1 Protocol and Graph Store endpoints over the served path (R857).

The app is the real served composition (``compose_served_invocation``): every
request authenticates, then drives ``query.sparql`` or
``eg.graph.ApplyMutation`` through the invoke chokepoint to a recording
engine client. The tests assert the engine calls, the negotiated formats and
the write-scope refusal.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from arbora.access.action_policy import ActionPolicies
from arbora.api.http.sparql import SPARQL_UPDATE_EVENT
from arbora.core.settings import runtime_config
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from tests.test_served_operation_api import FakeClient, FakePool, _actor

READ = ("sparql:read",)
WRITE = ("sparql:read", "graph:write")


class _EffectItems:
    """The engine work-item journal that holds each write once per key."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def submit(self, item: dict[str, Any]) -> dict[str, Any]:
        created = item["work_item_id"] not in self.rows
        self.rows.setdefault(
            item["work_item_id"],
            {"status": "queued", "metadata": dict(item["metadata"])},
        )
        return {"created": created}

    async def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        self.rows[request["work_item_id"]]["status"] = "leased"
        return {"claimed": True, "lease_epoch": 1, "fencing_token": 1}

    async def get(self, *, tenant: str, work_item_id: str) -> Any:
        return self.rows.get(work_item_id)

    async def cas_metadata(self, **request: Any) -> dict[str, Any]:
        self.rows[request["work_item_id"]]["metadata"] = request["set_metadata"]
        return {"outcome": "applied"}

    async def commit_result(self, **request: Any) -> dict[str, Any]:
        self.rows[request["work_item_id"]]["status"] = request["outcome"]
        return {"status": request["outcome"]}


class _RecordingEngine(FakeClient):
    """Answers ``Sparql`` with a fixed table and ``ApplyMutation`` with a receipt."""

    def __init__(self) -> None:
        super().__init__()
        self.work_items = _EffectItems()

    async def _send(
        self, method: str, params: Any, graph: str | None, **kwargs: Any
    ) -> Any:
        if method == "Sparql":
            self.sent.append((method, params, graph))
            if params["query"].lstrip().upper().startswith("CONSTRUCT"):
                return {
                    "vars": ["subject", "predicate", "object"],
                    "rows": [["<urn:a>", "<urn:p>", '"one"']],
                    "proofs": [],
                    "spatial": [],
                }
            return {
                "vars": ["s", "label"],
                "rows": [["urn:a", "one"], ["urn:b", None]],
                "proofs": [],
                "spatial": [],
            }
        if method == "ApplyMutation":
            self.sent.append((method, params, graph))
            return {"operations": 1, "inserted": 1, "deleted": 0}
        return await super()._send(method, params, graph, **kwargs)


def _app(monkeypatch: pytest.MonkeyPatch, roles: tuple[str, ...]) -> Any:
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "policy:fixture")
    monkeypatch.setattr(
        runtime_config, "auth_jwt_jwks_uri", "https://identity.invalid/jwks"
    )
    base = _actor()
    actor = replace(base, roles=(*base.roles, *roles))

    async def credential_owner(credential: str) -> Any:
        assert credential == "fixture-verified"  # sanitizer:ignore - synthetic
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    client = _RecordingEngine()
    process = request_identity.mint_graph_session(actor)
    invocation = compose_served_invocation(
        pool=FakePool(client),
        graph=process.graph,
        process_session=lambda: process,
        action_policies=ActionPolicies(),
        bindings={},
        fleet_effect=refused_fleet_effect,
    )
    app = FastAPI()
    invocation.mount_http(app, prefix="/api")
    return app, client


def _request(app: Any, method: str, path: str, **kwargs: Any) -> httpx.Response:
    headers = {"Authorization": "Bearer fixture-verified", **kwargs.pop("headers", {})}

    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://served"
        ) as http:
            return await http.request(method, path, headers=headers, **kwargs)

    return asyncio.run(call())


def _paths(app: Any) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}

    def walk(routes: Any, prefix: str) -> None:
        for route in routes:
            path = prefix + getattr(route, "path", "")
            if hasattr(route, "app") and hasattr(route.app, "routes"):
                walk(route.app.routes, path)
            elif getattr(route, "methods", None):
                found.setdefault(path, set()).update(route.methods)

    walk(app.routes, "")
    return found


@pytest.mark.spec("ARBORA-OPS-R857.1")
def test_routes_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _client = _app(monkeypatch, READ)
    paths = _paths(app)
    assert {"GET", "POST"} <= paths["/api/v1/sparql"]
    assert {"GET", "PUT", "POST", "DELETE"} <= paths["/api/v1/sparql/graphs"]


@pytest.mark.spec("ARBORA-OPS-R857.1")
def test_old_api_sparql_route_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.gateway import graph_api

    assert not hasattr(graph_api, "_mount_sparql_route")
    assert not hasattr(graph_api, "_get_sparql_bridge")
    import inspect

    assert "sparql" not in inspect.getsource(graph_api.register_graph_routes)


@pytest.mark.spec("ARBORA-OPS-R857.1")
def test_content_negotiation_and_not_acceptable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = _app(monkeypatch, READ)
    query = {"query": "SELECT ?s ?label WHERE { ?s ?p ?label }"}
    for accept, media in (
        ("application/sparql-results+xml", "application/sparql-results+xml"),
        ("text/csv", "text/csv"),
        ("text/tab-separated-values", "text/tab-separated-values"),
    ):
        response = _request(
            app, "GET", "/api/v1/sparql", params=query, headers={"Accept": accept}
        )
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith(media)
    csv_body = _request(
        app, "GET", "/api/v1/sparql", params=query, headers={"Accept": "text/csv"}
    ).text
    assert csv_body.splitlines()[:2] == ["s,label", "urn:a,one"]
    sent = len(client.sent)
    refused = _request(
        app, "GET", "/api/v1/sparql", params=query, headers={"Accept": "image/png"}
    )
    assert refused.status_code == 406
    assert len(client.sent) == sent


@pytest.mark.spec("ARBORA-OPS-R857", "ARBORA-OPS-R857.2")
def test_protocol_query_and_update(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = _app(monkeypatch, WRITE)

    query = _request(
        app,
        "POST",
        "/api/v1/sparql",
        content="SELECT ?s ?label WHERE { ?s ?p ?label }",
        headers={
            "Content-Type": "application/sparql-query",
            "Accept": "application/sparql-results+json",
        },
    )
    assert query.status_code == 200, query.text
    assert json.loads(query.text) == {
        "head": {"vars": ["s", "label"]},
        "results": {
            "bindings": [
                {
                    "s": {"type": "uri", "value": "urn:a"},
                    "label": {"type": "literal", "value": "one"},
                },
                {"s": {"type": "uri", "value": "urn:b"}},
            ]
        },
    }

    update = _request(
        app,
        "POST",
        "/api/v1/sparql",
        data={"update": "INSERT DATA { <urn:a> <urn:p> 'one' }"},
    )
    assert update.status_code == 204, update.text

    put = _request(
        app,
        "PUT",
        "/api/v1/sparql/graphs?default",
        content='<urn:a> <urn:p> "one" .',
        headers={"Content-Type": "application/n-triples"},
    )
    assert put.status_code == 204, put.text
    read = _request(app, "GET", "/api/v1/sparql/graphs?default")
    assert read.status_code == 200, read.text
    assert read.text == '<urn:a> <urn:p> "one" .\n'

    calls = [(method, params) for method, params, _graph in client.sent]
    assert [method for method, _ in calls] == [
        "Sparql",
        "ApplyMutation",
        "ApplyMutation",
        "Sparql",
    ]
    assert calls[1][1]["event_type"] == SPARQL_UPDATE_EVENT
    assert calls[1][1]["query"] == "INSERT DATA { <urn:a> <urn:p> 'one' }"
    assert calls[2][1]["query"].startswith("CLEAR DEFAULT; INSERT DATA {")
    # Each write is held once in the engine's effect journal.
    assert sorted(row["status"] for row in client.work_items.rows.values()) == [
        "succeeded",
        "succeeded",
    ]


@pytest.mark.spec("ARBORA-OPS-R857.2")
def test_update_without_write_scope_is_refused_before_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = _app(monkeypatch, READ)
    update = _request(
        app,
        "POST",
        "/api/v1/sparql",
        content="INSERT DATA { <urn:a> <urn:p> <urn:b> }",
        headers={"Content-Type": "application/sparql-update"},
    )
    assert update.status_code == 403
    assert update.json()["error"]["code"] == "SCOPE_REQUIRED"
    delete = _request(app, "DELETE", "/api/v1/sparql/graphs?default")
    assert delete.status_code == 403
    assert client.sent == []
