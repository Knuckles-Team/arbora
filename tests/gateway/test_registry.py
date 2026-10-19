"""Tests for arbora.gateway.registry — migrated from service-dashboard-core."""

import pytest

from arbora.gateway.registry import Registry, get_registry


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
