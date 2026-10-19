"""Tests for arbora.gateway.registry — migrated from service-dashboard-core."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from arbora.gateway import dashboard_api
from arbora.gateway.registry import _BUILTIN_WIDGETS, Registry, get_registry


def test_registry_singleton():
    reg1 = get_registry()
    reg2 = get_registry()
    assert reg1 is reg2


def test_list_all_known():
    reg = Registry()
    known = reg.list_all_known()
    assert "portainer" in known
    assert "uptime_kuma" in known
    assert "technitium" in known


def test_get_invalid_widget():
    reg = Registry()
    widget = reg.get_widget("non_existent_widget")
    assert widget is None


@pytest.mark.spec("ARBORA-OPS-R041.1")
def test_genius_agent_is_absent_from_the_widget_registry():
    """ARBORA-OPS-R041.1: the dropped constant-data widget types stay unregistered."""
    known = Registry().list_all_known()
    assert "genius_agent" not in known
    # genius_agent and agent_summary are the dropped constant-data widget types.
    assert not (set(known) & {"genius_agent", "agent_summary"})


@pytest.mark.spec("ARBORA-OPS-R039.8")
def test_widgets_route_serves_every_builtin_widget_type():
    """ARBORA-OPS-R039.8: GET /widgets returns the full builtin map, 6-field pinned."""
    app = FastAPI()
    dashboard_api.register_dashboard_routes(app)
    client = TestClient(app)

    response = client.get("/api/dashboard/widgets")

    assert response.status_code == 200
    items = response.json()
    served = {item["widget_type"] for item in items}
    assert served == set(_BUILTIN_WIDGETS)
    assert "genius_agent" not in served
    for item in items:
        assert set(item) == {
            "widget_type",
            "display_name",
            "icon",
            "category",
            "description",
            "supports_websocket",
        }
