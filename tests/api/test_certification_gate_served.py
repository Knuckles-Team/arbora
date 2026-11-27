"""Certification attestation and upgrade admission (ARBORA-OPS-R872, R875)."""

from __future__ import annotations

import pytest

from arbora.api.invoke import certification_gate as gate
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _decide
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context

SKILL = {"id": "s1", "revision": 3, "digest": "d3"}


@pytest.mark.spec("ARBORA-OPS-R872")
async def test_each_attestation_case_refuses_without_effect() -> None:
    for stored, reason in (
        (None, "attestation_missing"),
        ({"skill_id": "s1", "revision": 2, "digest": "d3"}, "attestation_stale"),
        ({"skill_id": "s1", "revision": 3, "digest": "dX"}, "attestation_mismatch"),
    ):

        async def read(skill: dict, stored=stored):
            return stored

        with pytest.raises(OperationRefused) as refused:
            await gate.require_attestation(engine_context(), [SKILL], read=read)
        assert refused.value.code == "POLICY_DENIED"
        assert refused.value.details["reason"] == reason

    async def exact(skill: dict):
        return {"skill_id": "s1", "revision": 3, "digest": "d3"}

    await gate.require_attestation(engine_context(), [SKILL], read=exact)


@pytest.mark.spec("ARBORA-OPS-R875")
async def test_breaking_stops_and_compatible_and_migrated_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    outcome = {"value": "breaking"}
    engine.serve("Decide", lambda p: {"receipt": {"outcome": outcome["value"]}})
    engine.install(_decide)
    context = engine_context()
    for value in ("compatible", "migrated"):
        outcome["value"] = value
        await gate.require_upgrade_admission(context, SKILL)
    outcome["value"] = "breaking"
    with pytest.raises(OperationRefused) as refused:
        await gate.require_upgrade_admission(context, SKILL)
    assert refused.value.details["reason"] == "upgrade_not_admitted"
    sent = engine.sent[0][1]["request"]["question"]
    assert sent["kind"] == "skill_upgrade"
