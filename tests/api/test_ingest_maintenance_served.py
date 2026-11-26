"""Admitted ingest maintenance (ARBORA-OPS-R886)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ingest
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context

_OP = next(op for op in ingest.operations() if op.id == "ingest.maintenance.run")
_BASE = {
    "corpus": "docs",
    "window_end_ms": 5,
    "expected_revision": 3,
    "cost_ceiling": 10,
}


def _engine(
    monkeypatch: pytest.MonkeyPatch, actions: list[dict[str, Any]]
) -> EngineDouble:
    engine = EngineDouble(monkeypatch)
    engine.serve("MeasureIngestThroughput", lambda p: {"lag": 4})
    engine.serve("Decide", lambda p: [{"admitted_actions": actions}])
    engine.serve("ReindexCorpus", lambda p: {"ok": True})
    engine.install(pin_lag)
    return engine


@pytest.mark.spec("ARBORA-OPS-R886")
async def test_maintenance_run_refuses_without_authorization_and_applies_admitted_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actions = [
        {
            "method": "ReindexCorpus",
            "params": {"corpus": "docs"},
            "cost": 4,
            "revision": 3,
        }
    ]
    engine = _engine(monkeypatch, actions)
    context = engine_context()
    with pytest.raises(OperationRefused) as missing:
        await ingest.run_maintenance(context, dict(_BASE), _OP)
    assert missing.value.code == "POLICY_DENIED" and engine.sent == []

    done = await ingest.run_maintenance(
        context, {**_BASE, "owner_authorization": "owner-1"}, _OP
    )
    assert [m for m, _, _ in engine.sent] == [
        "MeasureIngestThroughput",
        "Decide",
        "ReindexCorpus",
    ]
    applied = engine.sent[-1][1]
    assert applied["authorization"] == "owner-1" and applied["expected_revision"] == 3
    assert done["value"] == {
        "applied": [{"method": "ReindexCorpus", "cost": 4}],
        "cost_remaining": 6,
    }

    # Revision and cost are checked before the effect.
    engine.sent.clear()
    stale = _engine(monkeypatch, [{**actions[0], "revision": 2}])
    with pytest.raises(EngineRefusal):
        await ingest.run_maintenance(
            context, {**_BASE, "owner_authorization": "o"}, _OP
        )
    assert all(m != "ReindexCorpus" for m, _, _ in stale.sent)
    dear = _engine(monkeypatch, [{**actions[0], "cost": 11}])
    with pytest.raises(OperationRefused):
        await ingest.run_maintenance(
            context, {**_BASE, "owner_authorization": "o"}, _OP
        )
    assert all(m != "ReindexCorpus" for m, _, _ in dear.sent)
    assert ingest.NATIVE_IDEMPOTENCY["ingest.maintenance.run"]
