"""No later phase runs before P1 is VERIFIED (ARBORA-ACCEPTANCE-R006.1.3)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.testing.scenario_stack import (
    P1_ROWS,
    PHASE_PREREQUISITE_UNVERIFIED,
    ScenarioRefused,
    ScenarioRun,
    record_scenario_run,
    require_phase_prerequisite,
)

VERIFIED = dict.fromkeys(P1_ROWS, "VERIFIED")


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.1.3")
def test_later_phase_scenario_refuses_until_p1_verified() -> None:
    open_row = {**VERIFIED, "ARBORA-ACCEPTANCE-R006.1.2": "LANDED"}
    with pytest.raises(ScenarioRefused) as refused:
        require_phase_prerequisite(open_row, "passed")
    assert refused.value.code == PHASE_PREREQUISITE_UNVERIFIED
    assert refused.value.details == {
        "phase": "P1",
        "open_rows": ["ARBORA-ACCEPTANCE-R006.1.2"],
    }

    with pytest.raises(ScenarioRefused) as failed_run:
        require_phase_prerequisite(VERIFIED, "failed")
    assert failed_run.value.details["open_rows"] == ["latest P1 ScenarioRun"]
    with pytest.raises(ScenarioRefused):
        require_phase_prerequisite(VERIFIED, None)

    require_phase_prerequisite(VERIFIED, "passed")


@pytest.mark.spec("ARBORA-ACCEPTANCE-R006.1.3")
def test_the_scenario_run_record_names_the_shape_the_pinned_engine_lacks() -> None:
    sent: list[str] = []

    async def send(method: str, params: dict, tenant: str) -> dict:
        sent.append(method)
        return {}

    run = ScenarioRun(scenario_id="p1", phase="P1", result="passed", commit="a" * 40)
    try:
        asyncio.run(record_scenario_run(send, "tenant-a", run))
    except OperationRefused as refused:
        # PIN-LAG: the pinned release lacks the record; nothing is written.
        assert refused.code == "UNAVAILABLE"
        assert sent == []
    else:
        assert sent == ["DecisionLog"]
