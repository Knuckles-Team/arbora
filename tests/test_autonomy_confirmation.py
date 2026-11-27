"""Medulla's autonomy verdict blocks an action until approval (ARBORA-OPS-R867)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.autonomy import require_autonomy_clearance


def _ask(verdict: Any, calls: list[str]):
    async def ask(op_id: str, tenant: str, irreversible: bool) -> str:
        calls.append(op_id)
        if isinstance(verdict, Exception):
            raise verdict
        return verdict

    return ask


async def _clear(verdict: Any, *, approved: bool = False, read: bool = False):
    calls: list[str] = []
    refusal = await require_autonomy_clearance(
        _ask(verdict, calls),
        op_id="fleet.call",
        tenant="t",
        is_read=read,
        plan_approved=approved,
    )
    return refusal, calls


@pytest.mark.spec("ARBORA-OPS-R867")
async def test_irreversible_action_waits_for_approval() -> None:
    assert (await _clear("allow"))[0] is None
    refused, calls = await _clear("require_confirmation")
    assert refused.code == "CONFIRMATION_REQUIRED" and calls == ["fleet.call"]
    assert (await _clear("require_confirmation", approved=True))[0] is None
    down, _ = await _clear(RuntimeError("down"))
    assert down.code == "UNAVAILABLE"
    assert down.details == {"reason": "autonomy_verdict_unavailable"}
    unknown, _ = await _clear("Surprise")
    assert unknown.code == "UNAVAILABLE"
    # Reads never ask; no bound reader means the gate is not composed yet.
    refused, calls = await _clear("require_confirmation", read=True)
    assert refused is None and calls == []
    assert (
        await require_autonomy_clearance(
            None, op_id="x", tenant="t", is_read=False, plan_approved=False
        )
    ) is None
