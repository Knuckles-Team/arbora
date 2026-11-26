"""The evolution operations served through the real invoke pipeline (ARBORA-OPS-R022.2)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import person, route_methods
from tests.test_served_operation_api import _call, _value, served  # noqa: F401


class Loops:
    """One engine's loop state, keyed by loop id."""

    def __init__(self) -> None:
        self.state = {"L1": "running"}
        self.jobs: dict[str, str] = {}
        self.calls: list[str] = []

    def status(self, params: dict[str, Any], principal: str) -> Any:
        self.calls.append("LoopStatus")
        return {
            "state": self.state[params["loop_id"]],
            "last_run_ms": 1,
            "next_run_ms": 2,
        }

    def run(self, params: dict[str, Any], principal: str) -> Any:
        self.calls.append("LoopRun")
        return {"job_id": f"job-{len(self.jobs) + 1}"}

    def pause(self, params: dict[str, Any], principal: str) -> Any:
        self.calls.append("LoopPause")
        self.state[params["loop_id"]] = "paused"
        return {"state": "paused"}


@pytest.fixture
def loops(served: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Loops]:  # noqa: F811
    http, client = served
    engine = Loops()
    route_methods(
        client,
        {
            "LoopStatus": engine.status,
            "LoopRun": engine.run,
            "LoopPause": engine.pause,
        },
        monkeypatch,
        {
            "fixture-verified": person(
                "one", extra_scopes=("loops:read", "loops:control")
            ),
            "fixture-reader": person("two", extra_scopes=("loops:read",)),
        },
    )
    return http, engine


@pytest.mark.spec("ARBORA-OPS-R022.2")
@pytest.mark.spec("ARBORA-OPS-R022.2.1")
@pytest.mark.spec("ARBORA-OPS-R022.2.4")
@pytest.mark.spec("ARBORA-OPS-R022.2.5")
def test_scopes_idempotent_run_and_pause(loops: tuple[Any, Loops]) -> None:
    http, engine = loops
    status = _value(_call(http, "evolution.loops.status", {"loop_id": "L1"}))
    assert status == {"state": "running", "last_run_ms": 1, "next_run_ms": 2}
    key = {"Idempotency-Key": "run-1"}
    ran = _value(_call(http, "evolution.loops.run", {"loop_id": "L1"}, **key))
    assert ran == {"job_id": "job-1"}
    missing_key = _call(http, "evolution.loops.run", {"loop_id": "L1"})
    assert missing_key.status_code == 400
    reader = {"Authorization": "Bearer fixture-reader"}
    refused = _call(http, "evolution.loops.run", {"loop_id": "L1"}, **key, **reader)
    assert refused.status_code == 403
    paused = _value(
        _call(
            http,
            "evolution.loops.pause",
            {"loop_id": "L1"},
            **{"Idempotency-Key": "p1"},
        )
    )
    assert paused == {"state": "paused"}
    assert (
        _value(_call(http, "evolution.loops.status", {"loop_id": "L1"}))["state"]
        == "paused"
    )
