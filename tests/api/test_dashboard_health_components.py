"""Connector component states on the health endpoint (ARBORA-MESSAGING-R005.1.1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.observability import runtime_health as rh
from arbora.observability.state import build_health_state


def _entry(component_id: str, state: str, status: str, enabled: bool) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "component_id": component_id,
        "state": state,
        "configuration_status": status,
        "enabled": enabled,
        "last_check_time": datetime(2026, 11, 26, tzinfo=UTC),
    }
    if status == "disabled":
        entry["alert_suppression_reason"] = "operator disabled"
    return entry


@pytest.fixture
def cfg() -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(health=build_health_state())


@pytest.mark.spec("ARBORA-MESSAGING-R005.1.1")
def test_unconfigured_connector_raises_no_alert(cfg: Any) -> None:
    bodies = {
        "mail": {"components": [_entry("smtp", "not_configured", "disabled", False)]},
        "chat": {"components": [_entry("relay", "healthy", "initialized", True)]},
    }
    cfg.health.probes.bind(
        "connector_components", rh.connector_components_probe(lambda s: bodies)
    )
    check = rh._check_connector_components(cfg)
    assert check["status"] == "ok"
    assert check["detail"]["alerts"] == []

    bodies["chat"] = {
        "components": [_entry("relay", "unavailable", "initialized", True)]
    }
    check = rh._check_connector_components(cfg)
    assert check["status"] == "degraded"
    assert [a["component"] for a in check["detail"]["alerts"]] == ["relay"]
    # A degraded connector never flips the readiness rollup.
    assert check["status"] != "unhealthy"


@pytest.mark.spec("ARBORA-MESSAGING-R005.1.1")
def test_malformed_component_entry_is_never_read_as_healthy(cfg: Any) -> None:
    bodies = {"chat": {"components": [{"component_id": "x", "state": "healthy"}]}}
    probe = rh.connector_components_probe(lambda s: bodies)
    with pytest.raises(ValidationError):
        probe(None)
    cfg.health.probes.bind("connector_components", probe)
    result = rh._run_bounded(
        "connector_components", lambda: rh._check_connector_components(cfg)
    )
    assert result["status"] == "unhealthy"
