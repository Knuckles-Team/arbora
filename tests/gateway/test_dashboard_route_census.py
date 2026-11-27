"""The dashboard router's exact method and path pairs (ARBORA-OPS-R039.1)."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from arbora.gateway.dashboard_api import build_dashboard_router

PINNED = {
    ("GET", "/layout"),
    ("PUT", "/layout"),
    ("GET", "/data"),
    ("GET", "/data/{service_id}"),
    ("GET", "/full"),
    ("GET", "/data-subset"),
    ("GET", "/widgets"),
    ("GET", "/health"),
    ("GET", "/readiness"),
    ("GET", "/discover"),
    ("GET", "/daemon/status"),
    ("GET", "/daemon/shards"),
    ("GET", "/hydration-status"),
    ("POST", "/daemon/start"),
    ("POST", "/hydrate"),
    ("POST", "/hydrate/{source}"),
}


@pytest.mark.spec("ARBORA-OPS-R039.1")
def test_dashboard_router_exposes_every_route() -> None:
    pairs = {
        (method, route.path)
        for route in build_dashboard_router().routes
        if isinstance(route, APIRoute)
        for method in (route.methods or ())
    }
    assert len(PINNED) == 16
    assert pairs == PINNED, {"added": pairs - PINNED, "removed": PINNED - pairs}
