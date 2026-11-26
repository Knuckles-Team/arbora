"""The scheduled fleet smoke sweep (ARBORA-FLEET-R053)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.fleet.smoke_sweep import (
    FleetSmokeService,
    SmokeRow,
    SweepRefused,
    pick_probe,
    run_sweep,
    smoke_interval_s,
)

RO = {"readOnlyHint": True}


class Fleet:
    def __init__(self) -> None:
        self.failing = {"bad"}
        self.calls: list[tuple[str, str]] = []
        self.catalog = {
            "good": [
                {"name": "write_it", "annotations": {}, "inputSchema": {}},
                {"name": "ping", "annotations": RO, "inputSchema": {}},
            ],
            "bad": [{"name": "ping", "annotations": RO, "inputSchema": {}}],
            "gated": [{"name": "ping", "annotations": RO, "inputSchema": {}}],
            "bare": [
                {
                    "name": "needs_arg",
                    "annotations": RO,
                    "inputSchema": {"required": ["x"]},
                }
            ],
        }

    def servers(self):
        return list(self.catalog)

    def tools(self, server):
        return self.catalog[server]

    async def call(self, server: str, tool: str) -> Any:
        self.calls.append((server, tool))
        if server == "gated":
            raise SweepRefused("SCOPE_DENIED", "no grant  " + "x" * 500)
        if server in self.failing:
            raise RuntimeError("secret-token-123")
        return {"ok": True}


@pytest.mark.spec("ARBORA-FLEET-R053")
def test_sweep_records_rows_and_alerts_once_on_new_failure() -> None:
    fleet = Fleet()
    recorded: list[Any] = []
    alerts: list[SmokeRow] = []
    now = [0.0]
    svc = FleetSmokeService(
        fleet,
        record=recorded.append,
        alert=alerts.append,
        clock=lambda: now[0],
        interval_s=900,
    )
    fleet.failing = set()
    first = asyncio.run(svc.tick())
    assert first is not None
    by = {r.server: r for r in first}
    assert by["good"].outcome == "ok" and by["good"].tool == "ping"
    assert by["gated"].outcome == "refused" and by["gated"].error_code == "SCOPE_DENIED"
    assert len(by["gated"].detail) <= 200
    assert by["bare"].outcome == "no_probe" and by["bare"].tool is None
    assert alerts == []  # never passed before, so no alert
    # Only probe tools were ever called.
    assert all(tool == "ping" for _s, tool in fleet.calls)

    assert asyncio.run(svc.tick()) is None  # interval not elapsed
    now[0] = 901
    fleet.failing = {"bad"}
    second = asyncio.run(svc.tick())
    assert second is not None
    assert [a.server for a in alerts] == ["bad"]
    assert "secret" not in alerts[0].detail
    now[0] = 1802
    asyncio.run(svc.tick())
    assert [a.server for a in alerts] == ["bad"]  # fired once, not again
    assert len(recorded) == 3


@pytest.mark.spec("ARBORA-FLEET-R053")
def test_probe_choice_and_interval_bounds() -> None:
    assert pick_probe([{"name": "a", "annotations": {}}]) is None
    assert (
        pick_probe([{"name": "z", "annotations": RO}, {"name": "b", "annotations": RO}])
        == "b"
    )
    assert smoke_interval_s(None) == 900
    assert smoke_interval_s(5) == 60 and smoke_interval_s(10**9) == 86_400
    assert smoke_interval_s("junk") == 900


@pytest.mark.spec("ARBORA-FLEET-R053")
def test_run_sweep_refusal_is_not_an_alert_without_a_prior_pass() -> None:
    fleet = Fleet()
    alerts: list[SmokeRow] = []
    rows = asyncio.run(
        run_sweep(
            fleet,
            previous={"gated": "refused"},
            record=lambda r: None,
            alert=alerts.append,
        )
    )
    assert alerts == [] and len(rows) == 4
