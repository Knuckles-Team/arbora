"""The loop continuation survives a host restart (ARBORA-OPS-R876)."""

from __future__ import annotations

import pytest

from arbora.api.ops import loops
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context


@pytest.mark.spec("ARBORA-OPS-R876")
async def test_same_continuation_after_host_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = [{"continuation": {"next": "step-4"}, "kind": "FeedbackLoop"}]
    engine = EngineDouble(monkeypatch)
    engine.serve("DecisionLog", lambda p: committed)
    engine.install(loops)
    ops = {op.id: op for op in loops.operations()}
    first = await loops.handle_loops_status(
        engine_context(), {"loop_id": "L1"}, ops["loops.status"]
    )
    # A restart drops every process object; the engine record is the only state.
    second = await loops.handle_loops_run(
        engine_context(), {"loop_id": "L1"}, ops["loops.run"]
    )
    paused = await loops.handle_loops_pause(
        engine_context(), {"loop_id": "L1"}, ops["loops.pause"]
    )
    assert (
        first["value"]["continuation"]
        == second["value"]["continuation"]
        == {"next": "step-4"}
    )
    assert paused["value"]["continuation"] == {"next": "step-4"}
    assert engine.sent[0][1]["op"]["subject"] == "L1"
    assert set(loops.NATIVE_IDEMPOTENCY) == {"loops.run", "loops.pause"}
