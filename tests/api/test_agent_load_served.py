"""The ``agents.run`` load profile driver (ARBORA-CAPACITY-R004).

The driver is exercised with a controlled clock and a scripted run function:
the measurement, the thresholds and the refusals are the driver's own. Whether
a deployed release meets the profile is qualified by running the driver against
that deployment (``scripts/agent_load.py --base-url``), not by this test.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "agent_load.py"
REVISIONS = {"medulla": "1.2.3", "arbora": "abc123"}


def _driver() -> Any:
    spec = importlib.util.spec_from_file_location("agent_load", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _runner(clock: Clock, latencies_ms: list[float], fail_every: int = 0):
    state = {"n": 0}

    async def run_once() -> bool:
        state["n"] += 1
        clock.now += latencies_ms[(state["n"] - 1) % len(latencies_ms)] / 1000.0
        return not (fail_every and state["n"] % fail_every == 0)

    return run_once


@pytest.mark.spec("ARBORA-CAPACITY-R004")
async def test_agent_run_profile_measures_terminal_latency_and_failures() -> None:
    driver, clock = _driver(), Clock()
    report = await driver.profile(
        _runner(clock, [100.0, 200.0, 300.0]),
        requests=1000,
        workload="agents.run terminal latency",
        fixture="scripted-run",
        owner_revisions=REVISIONS,
        clock=clock,
    )
    assert [lv["concurrency"] for lv in report["levels"]] == [1, 8, 32]
    assert all(lv["requests"] == 1000 for lv in report["levels"])
    assert all(
        lv["p95_ms"] <= 2000 and lv["error_rate"] == 0 for lv in report["levels"]
    )
    assert report["verdict"] == "pass"
    assert (
        report["owner_revisions"] == REVISIONS and report["fixture"] == "scripted-run"
    )


@pytest.mark.spec("ARBORA-CAPACITY-R004")
async def test_a_timeout_counts_as_a_failure_and_exceeded_limits_fail_the_level() -> (
    None
):
    driver, clock = _driver(), Clock()

    async def sometimes_times_out() -> bool:
        clock.now += 0.05
        if int(clock.now / 0.05) % 20 == 0:
            raise TimeoutError
        return True

    # 5 percent timeouts breach the 1 percent failure limit.
    samples = await driver.measure(
        sometimes_times_out, concurrency=8, requests=1000, clock=clock
    )
    failed = driver.level_report(8, 1000, samples)
    assert failed["verdict"] == "fail" and failed["error_rate"] > 0.01
    slow = driver.level_report(1, 3, [(2500.0, True)] * 3)
    assert slow["verdict"] == "fail" and slow["p95_ms"] == 2500.0
    edge = driver.level_report(1, 2, [(2000.0, True)] * 2)
    assert edge["verdict"] == "pass"
    report = driver.qualify(
        [failed, slow], workload="w", fixture="f", owner_revisions=REVISIONS
    )
    assert report["verdict"] == "fail"


@pytest.mark.spec("ARBORA-CAPACITY-R004")
async def test_agent_run_profile_rejects_missing_samples_and_exceeded_limits() -> None:
    driver = _driver()
    with pytest.raises(driver.QualificationRefused) as short:
        driver.level_report(8, 1000, [(10.0, True)] * 999)
    assert short.value.code == "QUALIFICATION_REFUSED"
    assert short.value.reasons == ["missing_samples:8"]
    ok_level = driver.level_report(1, 1, [(10.0, True)])
    for kwargs, reason in (
        ({"workload": ""}, "missing_workload"),
        ({"fixture": ""}, "missing_fixture"),
        ({"owner_revisions": {}}, "missing_owner_revisions"),
        ({"owner_revisions": {"medulla": ""}}, "missing_owner_revisions"),
    ):
        inputs = {
            "workload": "w",
            "fixture": "f",
            "owner_revisions": REVISIONS,
        } | kwargs
        with pytest.raises(driver.QualificationRefused) as refused:
            driver.qualify([ok_level], **inputs)
        assert refused.value.reasons == [reason]
    with pytest.raises(driver.QualificationRefused):
        driver.qualify([], workload="w", fixture="f", owner_revisions=REVISIONS)
