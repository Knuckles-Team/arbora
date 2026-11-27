"""The in-process start path stays unchanged (ARBORA-DEPLOY-R017.7)."""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from arbora.access.action_policy import ActionPolicies
from arbora.browser_control.browser_state import BrowserState
from arbora.mcp_server import composition
from arbora.mcp_server.composition import CompositionPlan, start_composed_services
from arbora.mcp_server.host_caches import ToolTable


# spec: ARBORA-DEPLOY-R017.7
def test_live_start_calls_start_composed_services_with_detected_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Start-up detects one plan and starts exactly the co-services it names.

    No placement call, plan type or wrapper sits in front of the start.
    """
    detected: list[dict[str, Any]] = []
    started: list[str] = []
    plan = CompositionPlan(
        messaging_platforms=("telegram",),
        messaging_intake_enabled=True,
        intake_platforms=("telegram",),
    )

    def detect(engine: Any, **kwargs: Any) -> CompositionPlan:
        detected.append({"engine": engine, **kwargs})
        return plan

    class Supervisor:
        def start_service(self, name: str, *_args: Any) -> None:
            started.append(name)

    monkeypatch.setattr(composition, "detect_composition", detect)
    monkeypatch.setattr(composition, "CoServiceSupervisor", Supervisor)
    monkeypatch.setattr(
        composition,
        "start_messaging_intake",
        lambda _supervisor, _engine, _session, platforms: started.append(
            "messaging:" + ",".join(platforms)
        ),
    )
    engine = object()

    supervisor = start_composed_services(
        object(),
        engine,
        action_policies=ActionPolicies(),
        tools=ToolTable(),
        browser=BrowserState(),
        messaging_intake_enabled=True,
        served_network=True,
    )

    assert isinstance(supervisor, Supervisor)
    assert detected == [
        {"engine": engine, "messaging_intake_enabled": True, "served_network": True}
    ]
    assert started == ["messaging:telegram"]
    source = inspect.getsource(start_composed_services)
    assert "placement" not in source
    assert "ask_placement" not in inspect.getsource(composition)
