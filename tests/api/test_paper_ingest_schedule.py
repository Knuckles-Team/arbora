"""Arbora's side of the six-hour paper intake contract (ARBORA-OPS-R022.3).

Arbora is the schedule facade: it forwards the cadence, the exclusion group and
the idempotency key to the engine job scheduler as the verified caller and holds
no schedule, relevance or admission state. The durable six-hour clock, the
missed and retried run semantics and the relevance and admission verdicts are
owned by the engine, the SDK and the harness.
"""

from __future__ import annotations

import pytest

from arbora.api.ops import schedules
from tests.api.test_schedule_ops import OPS, _Client, _context, _serve

SIX_HOURS_S = 6 * 60 * 60


@pytest.mark.spec("ARBORA-OPS-R022.3")
@pytest.mark.asyncio
async def test_six_hour_clock_dedupe_restart_and_exclusions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    client = _Client()
    context = _context(client, monkeypatch)
    params = schedules.ScheduleWriteParams(
        name="paper-intake",
        task_kind="paper_intake",
        spec={"source": "papers"},
        period_s=SIX_HOURS_S,
        exclusion_group="intake",
        min_spacing_s=300,
    ).model_dump()
    for _ in range(2):  # a restart retries the same create under the same key
        await schedules.handle_schedule(context, params, OPS["schedules.create"])
    bodies = [sent["op"]["ScheduleCreate"] for _method, sent, _graph in client.sent]
    assert bodies[0] == bodies[1]  # one identical request: the engine dedupes by key
    assert bodies[0]["period_s"] == SIX_HOURS_S
    assert (bodies[0]["exclusion_group"], bodies[0]["min_spacing_s"]) == (
        "intake",
        300,
    )
    # Arbora holds no relevance, admission or selected-claim state of its own.
    assert not {"relevance_score", "admitted", "selected_claim_receipt"} & set(
        bodies[0]
    )
    assert OPS["schedules.create"].idempotency.name == "KEY_REQUIRED"
