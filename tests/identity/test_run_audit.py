"""One audit event per run (ARBORA-IDENTITY-R026.4)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.ops.agents import _audit_run
from tests.identity._session_fakes import caller, context


@pytest.mark.spec("ARBORA-IDENTITY-R026.4")
def test_one_event_per_run_with_all_fields() -> None:
    ctx = context(caller(engine_claims={"session_ref": "sess-7"}))
    op = type("Op", (), {"id": "agents.run"})()
    asyncio.run(_audit_run(ctx, op, {}, {"id": "run-1", "trace_id": "t-1"}))
    asyncio.run(_audit_run(ctx, op, {}, {}, failed=True))
    assert len(ctx.events) == 2
    for event in ctx.events:
        assert {"trace_id", "principal", "session_id", "run_id", "tenant"} <= set(event)
    assert ctx.events[0]["run_id"] == "run-1"
    assert ctx.events[1]["result_status"] == "failed"
    assert "prompt" not in repr(ctx.events)
