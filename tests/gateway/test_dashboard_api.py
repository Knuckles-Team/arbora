"""ARBORA-HOST-R019: Arbora serves the dashboard REST reads and stream.

overstory composes its ``/api`` surface from Arbora. These tests prove the
routes the frontend calls are mounted, answer with the aggregator's data, and
that ``/ws/dashboard`` accepts a handshake and streams snapshot/update frames.
No live engine or services: the aggregator and daemon are replaced by fakes.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbora.gateway import dashboard_api
from arbora.gateway.models import DashboardLayout, WidgetData

FRONTEND_PATHS = (
    "/api/dashboard/full",
    "/api/dashboard/health",
    "/api/dashboard/daemon/status",
    "/api/dashboard/daemon/shards",
)


class _FakeAggregator:
    def __init__(self) -> None:
        self.fetched: list[str] = []

    def get_layout(self) -> DashboardLayout:
        return DashboardLayout()

    def save_layout(self, layout: DashboardLayout) -> None:
        self.fetched.append("save_layout")

    async def fetch_all(self) -> dict[str, WidgetData]:
        return {"alpha": self._widget("alpha"), "beta": self._widget("beta")}

    async def fetch_one(self, service_id: str) -> WidgetData:
        self.fetched.append(service_id)
        return self._widget(service_id)

    @staticmethod
    def _widget(service_id: str) -> WidgetData:
        return WidgetData(fields={"id": service_id}, status="ok")


@pytest.fixture
def aggregator(monkeypatch: pytest.MonkeyPatch) -> _FakeAggregator:
    fake = _FakeAggregator()
    monkeypatch.setattr(dashboard_api, "_aggregator", fake)
    monkeypatch.setattr(dashboard_api, "_WS_PUSH_INTERVAL_SECONDS", 0.05)
    return fake


@pytest.fixture
def client(aggregator: _FakeAggregator) -> TestClient:
    app = FastAPI()
    dashboard_api.register_dashboard_routes(app)
    return TestClient(app)


def test_frontend_dashboard_paths_are_mounted() -> None:
    app = FastAPI()
    dashboard_api.register_dashboard_routes(app)
    paths = set(app.openapi()["paths"])

    assert set(FRONTEND_PATHS) <= paths
    assert any(getattr(route, "path", None) == "/ws/dashboard" for route in app.routes)


def test_registration_is_idempotent_for_the_websocket_route() -> None:
    app = FastAPI()
    dashboard_api.register_dashboard_routes(app)
    dashboard_api.register_dashboard_routes(app)

    ws_routes = [r for r in app.routes if getattr(r, "path", None) == "/ws/dashboard"]
    assert len(ws_routes) == 1


def test_graph_routes_compose_the_dashboard_surface() -> None:
    """register_graph_routes is the composer overstory calls."""
    import inspect

    from arbora.gateway import graph_api

    source = inspect.getsource(graph_api.register_graph_routes)
    assert "register_dashboard_routes(app, prefix=prefix)" in source


def test_full_returns_layout_and_data(client: TestClient) -> None:
    response = client.get("/api/dashboard/full")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"layout", "data"}
    assert set(body["data"]) == {"alpha", "beta"}


@pytest.mark.spec("ARBORA-OPS-R039.3")
def test_health_is_served_without_capabilities(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arbora.observability import runtime_health

    async def _report() -> dict[str, Any]:
        return {"status": "ok"}

    monkeypatch.setattr(runtime_health, "collect_health_async", _report)

    response = client.get("/api/dashboard/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


def test_unknown_service_id_shape_is_404(client: TestClient) -> None:
    assert client.get("/api/dashboard/data/..%2Fetc").status_code == 404


def test_websocket_streams_snapshot_then_scoped_update(
    client: TestClient, aggregator: _FakeAggregator
) -> None:
    with client.websocket_connect("/ws/dashboard") as ws:
        snapshot = ws.receive_json()
        ws.send_text('{"type": "subscribe", "widget_ids": ["beta"]}')
        update = ws.receive_json()

    assert snapshot["type"] == "snapshot"
    assert snapshot["sequence"] == 1
    assert set(snapshot["data"]) == {"alpha", "beta"}
    assert update["type"] == "update"
    assert update["stream_id"] == snapshot["stream_id"]
    assert update["sequence"] == 2
    assert set(update["data"]) == {"beta"}
    assert aggregator.fetched == ["beta"]


@pytest.mark.parametrize(
    "raw",
    ["not json", "[]", '{"type": "other"}', '{"type": "subscribe", "widget_ids": 1}'],
)
def test_malformed_subscription_is_ignored(raw: str) -> None:
    assert dashboard_api.parsed_widget_subscription(raw) is None


def test_subscription_drops_non_string_ids() -> None:
    raw = '{"type": "subscribe", "widget_ids": ["a", 2, "b"]}'
    assert dashboard_api.parsed_widget_subscription(raw) == {"a", "b"}


def _claims_client(
    monkeypatch: pytest.MonkeyPatch, capabilities: set[str]
) -> TestClient:
    """The dashboard app with caller claims taken from a header.

    ``x-caller: key`` is an API-key caller; ``x-caller: user`` holds the
    capabilities the identity mapping yields (replaced here).
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    from arbora.security import identity

    monkeypatch.setattr(
        identity, "base_capabilities", lambda _identity, _map: set(capabilities)
    )
    app = FastAPI()

    class Claims(BaseHTTPMiddleware):
        async def dispatch(self, request: Any, call_next: Any) -> Any:
            kind = request.headers.get("x-caller", "user")
            request.state.user_claims = {
                "sub": "person:one",
                "auth_type": "api_key" if kind == "key" else "oidc",
            }
            return await call_next(request)

    app.add_middleware(Claims)
    dashboard_api.register_dashboard_routes(app)
    return TestClient(app, raise_server_exceptions=False)


WRITE_ROUTES = (
    ("PUT", "/api/dashboard/layout"),
    ("POST", "/api/dashboard/daemon/start"),
    ("POST", "/api/dashboard/hydrate"),
    ("POST", "/api/dashboard/hydrate/github"),
)


def _write(client: TestClient, method: str, path: str, **headers: str) -> Any:
    body = DashboardLayout().model_dump(mode="json") if method == "PUT" else None
    return client.request(method, path, json=body, headers=headers)


@pytest.mark.spec("ARBORA-OPS-R039.2")
def test_write_routes_refuse_read_only_callers(
    aggregator: _FakeAggregator, monkeypatch: pytest.MonkeyPatch
) -> None:
    read_only = _claims_client(monkeypatch, {"gateway:read"})
    for method, path in WRITE_ROUTES:
        refused = _write(read_only, method, path)
        assert refused.status_code == 403, (method, path)
        assert refused.json()["detail"] == "dashboard write capability required"
    # An API-key caller carries no capability set and passes the check; the
    # engine-backed work behind the hydrate routes is refused after it.
    for method, path in WRITE_ROUTES:
        passed = _write(read_only, method, path, **{"x-caller": "key"})
        assert passed.status_code != 403, (method, path)
    writer = _claims_client(monkeypatch, {"gateway:write"})
    for method, path in WRITE_ROUTES:
        assert _write(writer, method, path).status_code != 403, (method, path)


@pytest.mark.spec("ARBORA-OPS-R039.4")
def test_malformed_source_is_422_and_unknown_service_is_404(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine_calls: list[str] = []
    monkeypatch.setattr(
        dashboard_api, "_active_engine", lambda: engine_calls.append("engine")
    )
    refused = client.post("/api/dashboard/hydrate/bad%20source")
    assert refused.status_code == 422
    assert refused.json()["detail"] == "invalid hydration source"
    too_long = client.post("/api/dashboard/hydrate/" + "a" * 129)
    assert too_long.status_code == 422
    assert engine_calls == []
    assert client.get("/api/dashboard/data/bad..id/x").status_code == 404


@pytest.mark.spec("ARBORA-OPS-R039.5")
def test_daemon_status_key_sets_are_pinned(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from arbora.gateway import daemon

    monkeypatch.setattr(daemon, "_engine", None)
    idle = client.get("/api/dashboard/daemon/status").json()
    assert set(idle) == {"running", "role"}
    assert idle["running"] is False

    class Engine:
        def unified_daemon_status(self) -> dict[str, Any]:
            return {
                "running": True,
                "role": "primary",
                "threads": 3,
                "jobs": 2,
                "queue_depth": 0,
            }

    monkeypatch.setattr(daemon, "_engine", Engine())
    busy = client.get("/api/dashboard/daemon/status").json()
    assert {"running", "role", "threads", "jobs", "queue_depth"} <= set(busy)
