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
from arbora.api.http.sparql import RDF_GRAPH_WRITE_EVENT, SPARQL_UPDATE_EVENT
from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.security import request_identity
from arbora.security.key_cache import JwksCache
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
        self.journal = _EffectItems()
        # The effect journal awaits its work-item calls.
        self.work_items = self.journal  # type: ignore[assignment]

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
    monkeypatch.setenv("AUTH_JWT_AUDIENCE", "arbora-fixture")
    monkeypatch.setenv("KG_POLICY_VERSION", "policy:fixture")
    monkeypatch.setenv("AUTH_JWT_JWKS_URI", "https://identity.invalid/jwks")
    base = _actor()
    actor = replace(base, roles=(*base.roles, *roles))

    async def credential_owner(credential: str, **_kw) -> Any:
        assert credential == "fixture-verified"  # sanitizer:ignore - synthetic
        return actor

    monkeypatch.setattr(request_identity, "actor_from_bearer_token", credential_owner)
    client = _RecordingEngine()
    process = request_identity.mint_graph_session(actor)
    invocation = compose_served_invocation(
        config=RuntimeConfig(),
        keys=JwksCache(),
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

    calls = [(method, params) for method, params, _graph in client.sent]
    assert [method for method, _ in calls] == [
        "Sparql",
        "ApplyMutation",
        "ApplyMutation",
    ]
    assert calls[1][1]["event_type"] == SPARQL_UPDATE_EVENT
    assert calls[1][1]["query"] == "INSERT DATA { <urn:a> <urn:p> 'one' }"
    # A graph write sends the serialized body to the engine, which parses it.
    assert calls[2][1]["event_type"] == RDF_GRAPH_WRITE_EVENT
    assert json.loads(calls[2][1]["query"]) == {
        "media_type": "application/n-triples",
        "mode": "replace",
        "body": '<urn:a> <urn:p> "one" .',
    }
    # The effect journal that held each write once per key is the invoke pipeline's
    # ``EffectJournal`` port now; its once-per-key proof lives with that port.


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


# --- the graph store carries six RDF media types (ARBORA-OPS-R857.3 to R857.5) ---------------

import re as _re  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from arbora.api.errors import EngineRefusal  # noqa: E402
from arbora.api.http import sparql as sparql_http  # noqa: E402
from arbora.security.operation_result import OpError

_SIX = (
    "application/n-triples",
    "text/turtle",
    "application/n-quads",
    "application/trig",
    "application/ld+json",
    "application/rdf+xml",
)
_BODIES = {media: f"body in {media}" for media in _SIX}


class _GraphEngine:
    """Stores one serialized graph per media type and answers the engine's refusals."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.body = ""
        self.write_scope = True

    async def invoke(
        self, op: str, params: dict[str, Any], caller: Any, surface: Any, **_: Any
    ) -> Any:
        if op == sparql_http.UPDATE_OP and not self.write_scope:
            return OpError("SCOPE_REQUIRED", {"missing_scopes": ["graph:write"]})
        self.calls.append((op, params))
        if op == sparql_http.UPDATE_OP:
            request = json.loads(params["query"])
            if request["body"] == "unparseable":
                return EngineRefusal("RDF_PARSE_FAILED")
            if request["body"] == "huge":
                return EngineRefusal("RDF_BODY_TOO_LARGE")
            if request["body"] == "quads":
                return EngineRefusal("RDF_DATASET_GRAPH_MISMATCH")
            if request["media_type"] == "application/x-unknown":
                return EngineRefusal("RDF_MEDIA_TYPE_UNSUPPORTED")
            self.body = request["body"]
            return SimpleNamespace(value={"operations": 1})
        return SimpleNamespace(
            value={"graph_body": f"{self.body}|{params['graph_media_type']}"}
        )


def _graph_app(engine: _GraphEngine, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    from medulla.generated import models

    monkeypatch.setattr(
        models.MethodSparqlParams,
        "model_fields",
        {**models.MethodSparqlParams.model_fields, "graph_media_type": object()},
    )

    async def authenticate(_request: Any) -> Any:
        return SimpleNamespace(request_id="req-1", session=None)

    app = FastAPI()
    sparql_http.mount_sparql_protocol(
        app,
        services=SimpleNamespace(registry=SimpleNamespace(digest="digest")),
        authenticate=authenticate,
        invoke=engine.invoke,
        max_result_bytes=1_000_000,
    )
    return app


def _go(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx.Response:
    async def call() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://graph"
        ) as http:
            return await http.request(method, path, **kwargs)

    return asyncio.run(call())


@pytest.mark.spec("ARBORA-OPS-R857.3")
def test_graph_store_round_trips_each_media_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert set(sparql_http.GRAPH_TYPES) == {*_SIX, "text/plain"}
    assert sparql_http.GRAPH_TYPES["text/plain"] == "application/n-triples"
    assert not hasattr(sparql_http, "render_ntriples")
    engine = _GraphEngine()
    app = _graph_app(engine, monkeypatch)
    for media in _SIX:
        put = _go(
            app,
            "PUT",
            "/sparql/graphs?default",
            content=_BODIES[media],
            headers={"Content-Type": media},
        )
        assert put.status_code == 204, put.text
        write = engine.calls[-1]
        assert write[0] == sparql_http.UPDATE_OP
        assert write[1]["event_type"] == RDF_GRAPH_WRITE_EVENT
        assert json.loads(write[1]["query"]) == {
            "media_type": media,
            "mode": "replace",
            "body": _BODIES[media],
        }
        read = _go(app, "GET", "/sparql/graphs?default", headers={"Accept": media})
        assert read.status_code == 200, read.text
        assert read.headers["content-type"].startswith(media)
        assert read.text == f"{_BODIES[media]}|{media}"
    merge = _go(
        app,
        "POST",
        "/sparql/graphs?default",
        content="x",
        headers={"Content-Type": "text/plain"},
    )
    assert merge.status_code == 204
    assert json.loads(engine.calls[-1][1]["query"])["mode"] == "merge"
    assert (
        json.loads(engine.calls[-1][1]["query"])["media_type"]
        == "application/n-triples"
    )
    # CONSTRUCT on /sparql is negotiated from the same six types.
    construct = _go(
        app,
        "GET",
        "/sparql",
        params={"query": "CONSTRUCT { ?s ?p ?o } WHERE { ?s ?p ?o }"},
        headers={"Accept": "text/turtle"},
    )
    assert construct.status_code == 200 and construct.text.endswith("|text/turtle")
    assert _re.search(r"graph_media_type", repr(engine.calls[-1]))


@pytest.mark.spec("ARBORA-OPS-R857.4")
def test_graph_store_refusals_use_engine_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _GraphEngine()
    app = _graph_app(engine, monkeypatch)
    before = len(engine.calls)
    unsupported = _go(
        app,
        "PUT",
        "/sparql/graphs?default",
        content="x",
        headers={"Content-Type": "application/x-unknown"},
    )
    assert unsupported.status_code == 415
    unacceptable = _go(
        app, "GET", "/sparql/graphs?default", headers={"Accept": "image/png"}
    )
    assert unacceptable.status_code == 406
    assert len(engine.calls) == before, "a local refusal makes no engine call"
    for body, status, code in (
        ("unparseable", 400, "RDF_PARSE_FAILED"),
        ("huge", 413, "RDF_BODY_TOO_LARGE"),
        ("quads", 400, "RDF_DATASET_GRAPH_MISMATCH"),
    ):
        refused = _go(
            app,
            "PUT",
            "/sparql/graphs?default",
            content=body,
            headers={"Content-Type": "text/turtle"},
        )
        assert refused.status_code == status, refused.text
        assert refused.json()["error"]["code"] == code
    from arbora.api.engine_errors import ENGINE_ERRORS

    assert ENGINE_ERRORS["RDF_MEDIA_TYPE_UNSUPPORTED"][0] == 415


@pytest.mark.spec("ARBORA-OPS-R857.3")
def test_a_pinned_engine_without_graph_media_type_refuses_the_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    if "graph_media_type" in models.MethodSparqlParams.model_fields:
        pytest.skip("the pin carries graph_media_type")
    engine = _GraphEngine()

    async def authenticate(_request: Any) -> Any:
        return SimpleNamespace(request_id="req-1", session=None)

    app = FastAPI()
    sparql_http.mount_sparql_protocol(
        app,
        services=SimpleNamespace(registry=SimpleNamespace(digest="digest")),
        authenticate=authenticate,
        invoke=engine.invoke,
        max_result_bytes=1_000_000,
    )
    read = _go(app, "GET", "/sparql/graphs?default")
    assert read.json()["error"]["code"] == "UNAVAILABLE"
    assert engine.calls == []


@pytest.mark.spec("ARBORA-OPS-R857.5")
def test_turtle_put_then_jsonld_get(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = _GraphEngine()
    app = _graph_app(engine, monkeypatch)
    turtle = "<urn:a> <urn:p> 'one' ."
    put = _go(
        app,
        "PUT",
        "/sparql/graphs?default",
        content=turtle,
        headers={"Content-Type": "text/turtle"},
    )
    assert put.status_code == 204
    read = _go(
        app, "GET", "/sparql/graphs?default", headers={"Accept": "application/ld+json"}
    )
    assert read.status_code == 200
    assert read.text == f"{turtle}|application/ld+json"
    engine.write_scope = False
    calls = len(engine.calls)
    denied = _go(
        app,
        "PUT",
        "/sparql/graphs?default",
        content=turtle,
        headers={"Content-Type": "text/turtle"},
    )
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "SCOPE_REQUIRED"
    assert len(engine.calls) == calls, "a refused write makes zero engine calls"
