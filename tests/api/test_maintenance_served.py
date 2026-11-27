"""Paper-shortfall maintenance refuses before any effect (ARBORA-OPS-R072, pin-lag)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import maintenance
from arbora.api.ops.registry_factory import get_registry
from tests.api._lane_double import EngineDouble, context

_PARAMS = {
    "owner_authorization_ref": "auth-1",
    "expected_revision": 3,
    "cost_ceiling": 500,
    "admission_verdict_ref": "verdict-1",
    "cost": 100,
}


def test_the_operation_is_absent_until_the_engine_publishes_the_method() -> None:
    assert maintenance.method_published() is False
    assert maintenance.operations() == ()
    assert get_registry().get("maintenance.paper_shortfall.run") is None


async def test_pre_effect_refusals_and_the_missing_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineDouble()
    ctx = context(client, monkeypatch)

    async def refused(params: dict) -> OperationRefused:
        with pytest.raises(OperationRefused) as caught:
            await maintenance.handle_paper_shortfall_run(ctx, params, None)  # type: ignore[arg-type]
        return caught.value

    assert (
        await refused({**_PARAMS, "owner_authorization_ref": None})
    ).code == "SCOPE_REQUIRED"
    over = await refused({**_PARAMS, "cost": 501})
    assert (
        over.code == "POLICY_DENIED" and over.details["reason"] == "cost_over_ceiling"
    )
    missing = await refused(_PARAMS)
    assert (
        missing.code == "UNAVAILABLE"
        and missing.details["missing"] == "PaperMaintenanceApply"
    )
    assert client.sent == []
