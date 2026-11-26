"""A registered connector serves all surfaces and follows its release (ARBORA-FLEET-R041.6)."""

from __future__ import annotations

import pytest

from arbora.fleet.onboarding import OnboardingTrigger
from tests.fleet.test_auto_onboard import _counted_pack
from tests.fleet.test_connect_contract import ENDPOINT, make_onboarding


@pytest.mark.spec("ARBORA-FLEET-R041.6")
@pytest.mark.asyncio
async def test_register_then_release_serves_all_surfaces_without_manual_step(
    monkeypatch,
) -> None:
    packs = [_counted_pack(prompts=1), _counted_pack(release=b"m2", prompts=2)]
    onboarding, imports = make_onboarding(packs, monkeypatch)
    await onboarding.onboard(
        ENDPOINT, auth=None, trigger=OnboardingTrigger("crm", "registered")
    )
    first = onboarding.surfaces["crm"]["counts"]
    assert first["tools"] == 2 and first["prompts"] == 1
    await onboarding.onboard(
        ENDPOINT, auth=None, trigger=OnboardingTrigger("crm", "release_changed", "d2")
    )
    second = onboarding.surfaces["crm"]
    assert second["counts"]["prompts"] == 2
    assert second["trigger"]["reason"] == "release_changed"
    assert len(imports) == 2
