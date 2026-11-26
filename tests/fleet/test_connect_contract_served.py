"""First connect imports every resource; a seed change reports only the seed (ARBORA-FLEET-R036.6)."""

from __future__ import annotations

import pytest

from tests.fleet.test_connect_contract import (
    ENDPOINT,
    contract_pack,
    make_onboarding,
)


@pytest.mark.spec("ARBORA-FLEET-R036.6")
@pytest.mark.asyncio
async def test_first_connect_and_list_changed_reimport_only_changed(
    monkeypatch,
) -> None:
    packs = [contract_pack(), contract_pack(seed=b"seed-v2")]
    onboarding, imports = make_onboarding(packs, monkeypatch)
    await onboarding.onboard(ENDPOINT, auth=None)
    first = onboarding.surfaces["crm"]["contract"]["changed"]
    assert first == ["manifest", "ontology:ontology:crm", "seed"] or set(first) == {
        "manifest",
        "ontology:ontology:crm",
        "seed",
    }
    await onboarding.onboard(ENDPOINT, auth=None)
    assert onboarding.surfaces["crm"]["contract"]["changed"] == ["seed"]
    assert len(imports) == 2
