"""Agent run status reads the canonical run state (ARBORA-OPS-R900)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from arbora import agent_runs
from arbora.agent_runs import UnknownRunStatus, canonical_run_status

CANONICAL = ("pending", "running", "completed", "stopped", "failed")


@pytest.mark.spec("ARBORA-OPS-R900")
def test_status_comes_from_the_canonical_state_and_unknown_is_refused() -> None:
    from meristem.graph.state import RunStatus

    assert agent_runs.canonical_run_statuses() == tuple(get_args(RunStatus))
    with pytest.raises(UnknownRunStatus) as refused:
        canonical_run_status("paused", "running")
    assert refused.value.code == "RUN_STATUS_UNKNOWN"
    assert refused.value.machine == "agent_run"
    assert (refused.value.from_state, refused.value.event) == ("running", "paused")


@pytest.mark.spec("ARBORA-OPS-R900")
@pytest.mark.parametrize("status", CANONICAL, ids=CANONICAL)
def test_each_canonical_status_is_accepted(status: str) -> None:
    assert canonical_run_status(status) == status


@pytest.mark.spec("ARBORA-OPS-R900")
def test_an_event_with_an_unknown_status_is_refused_before_it_is_stored() -> None:
    event = {"run_id": "r1", "type": "run.status", "step": 1, "status": "paused"}
    with pytest.raises(UnknownRunStatus):
        agent_runs.record_run_event(event)


@pytest.mark.spec("ARBORA-OPS-R900")
def test_no_literal_status_tuple_remains() -> None:
    source = Path(agent_runs.__file__).read_text(encoding="utf-8")
    assert not re.search(r'"completed",\s*"stopped"', source)
    assert not re.search(r'"stopped",\s*"failed"', source)
