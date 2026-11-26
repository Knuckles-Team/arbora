"""Arbora's side of two cross-owner definition slices (ARBORA-OPS-R022.4, R024.1.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.ops import evolution, telemetry


@pytest.mark.spec("ARBORA-OPS-R022.4")
def test_loop_facade_is_status_run_pause_with_fail_closed_scope_and_keys() -> None:
    ops = {
        op.id: op
        for op in evolution.operations()
        if op.id.startswith("evolution.loops.")
    }
    assert set(ops) == {
        "evolution.loops.status",
        "evolution.loops.run",
        "evolution.loops.pause",
    }
    assert ops["evolution.loops.status"].scopes == {"loops:read"}
    for op_id in ("evolution.loops.run", "evolution.loops.pause"):
        assert ops[op_id].scopes == {"loops:control"}
        assert ops[op_id].idempotency.name == "KEY_REQUIRED"
        assert op_id in evolution.NATIVE_IDEMPOTENCY  # the engine owns the replay
    # The run request names the loop only: checker findings, the next instruction
    # and the stop rule live in the harness and the engine, never in this facade.
    with pytest.raises(ValidationError):
        evolution.LoopRunParams(loop_id="loop-1", next_instruction="fix it")
    with pytest.raises(ValidationError):
        evolution.LoopRunParams(loop_id="loop-1", findings=["x"])


@pytest.mark.spec("ARBORA-OPS-R024.1.1")
def test_telemetry_read_serves_the_engine_snapshot_and_no_export_or_backend_selector() -> (
    None
):
    (op,) = telemetry.operations()
    assert op.scopes == {"telemetry:read"} and op.effect.value == "read"
    # Export formats, backend opt-in and the signal-to-proposal route are not
    # served from this operation: a request that names one is refused, never ignored.
    for field in ("format", "backend", "export", "propose"):
        with pytest.raises(ValidationError):
            telemetry.TelemetryReadParams(**{field: "otlp"})
