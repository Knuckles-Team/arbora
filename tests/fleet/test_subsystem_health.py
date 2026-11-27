"""Per-subsystem health (ARBORA-FLEET-R065) and the load probe verdict (R066)."""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

import pytest

from arbora.observability import subsystem_health as health
from arbora.observability.state import SubsystemWindows


@pytest.fixture
def windows() -> SubsystemWindows:
    return SubsystemWindows()


@pytest.mark.spec("ARBORA-FLEET-R065")
def test_classify_maps_verbs_to_subsystems() -> None:
    assert health.classify("find", None) == "discovery"
    assert health.classify("find", "catalog") == "discovery"
    assert health.classify("act", "fleet.call") == "fleet"
    assert health.classify("ask", "eg.graph.NodeCount") == "engine"


@pytest.mark.spec("ARBORA-FLEET-R065")
def test_wedged_subsystem_is_reported_alone(windows: SubsystemWindows) -> None:
    windows.begin("fleet", time.monotonic() - health.WEDGE_AFTER_S - 1)
    with health.track(windows, "engine"):
        pass
    snap = health.snapshot(windows)
    assert snap["subsystems"]["fleet"]["status"] == "wedged"
    assert snap["subsystems"]["engine"]["status"] == "ok"
    assert snap["subsystems"]["discovery"]["status"] == "ok"
    assert snap["status"] == "degraded"


@pytest.mark.spec("ARBORA-FLEET-R065")
def test_degraded_after_refusals(windows: SubsystemWindows) -> None:
    for _ in range(3):
        with health.track(windows, "discovery") as outcome:
            outcome["ok"] = False
    with pytest.raises(RuntimeError), health.track(windows, "discovery"):
        raise RuntimeError("boom")
    state = health.snapshot(windows)["subsystems"]["discovery"]
    assert state["status"] == "degraded"
    assert state["recent_failures"] == 4
    assert state["in_flight"] == 0


@pytest.mark.spec("ARBORA-FLEET-R066")
def test_load_summary_verdict() -> None:
    path = Path(__file__).parents[2] / "scripts" / "load_find_act.py"
    spec = importlib.util.spec_from_file_location("load_find_act", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    good = [("find", "ok", 0.2)] * 99 + [("act", "ok", 1.0)]
    assert module.summarize(good, 10.0)["ok"] is True
    hung = good + [("act", "hang", 60.0)]
    report = module.summarize(hung, 10.0)
    assert report["ok"] is False and report["hangs"] == 1
