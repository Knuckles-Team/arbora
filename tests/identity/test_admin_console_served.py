"""The mapping dry run and the mode wizard (ARBORA-IDENTITY-R007.2)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import identity as identity_ops
from arbora.identity.admin_console import (
    WIZARD_STEPS,
    AdminConsoleRefused,
    advance_wizard,
    dry_run_provider_rules,
)

RULES: list[dict[str, object]] = [
    {
        "rule_id": "r1",
        "claim_path": "groups",
        "match_kind": "equals",
        "value": "ops",
        "target": "role:operator",
    },
    {
        "rule_id": "r2",
        "claim_path": "groups",
        "match_kind": "prefix",
        "value": "eng-",
        "target": "group:engineers",
    },
    {
        "rule_id": "r3",
        "claim_path": "groups",
        "match_kind": "equals",
        "value": "ops",
        "target": "role:auditor",
    },
]


@pytest.mark.spec("ARBORA-IDENTITY-R007.2")
def test_tabs_dry_run_and_mode_transition(monkeypatch: pytest.MonkeyPatch) -> None:
    # The dry run takes the union of every matching rule and writes nothing.
    preview = dry_run_provider_rules(RULES, {"groups": ["ops", "eng-core"]})
    assert preview == {
        "roles": ["auditor", "operator"],
        "groups": ["engineers"],
        "matched_rules": ["r1", "r2", "r3"],
    }
    assert dry_run_provider_rules(RULES, {"groups": []})["matched_rules"] == []
    bad = [{**RULES[0], "match_kind": "regex"}]
    with pytest.raises(AdminConsoleRefused) as invalid:
        dry_run_provider_rules(bad, {"groups": ["ops"]})
    assert invalid.value.code == "MAPPING_RULE_INVALID"

    # The served operation refuses a stored invalid rule with the same code.
    async def listed(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return [{"idp_id": "idp", "rules": bad}]

    monkeypatch.setattr(identity_ops, "_send", listed)
    with pytest.raises(OperationRefused) as refused:
        asyncio.run(
            identity_ops.handle_providers_mapping_dry_run(
                SimpleNamespace(),
                {"provider_id": "idp", "claims": {}},
                None,  # type: ignore[arg-type]
            )
        )
    assert refused.value.code == "MAPPING_RULE_INVALID"

    # The wizard runs its fixed steps in order, each chained to the previous receipt.
    receipt = None
    seen = []
    for step in WIZARD_STEPS:
        receipt = advance_wizard(receipt, step)
        seen.append(receipt.digest)
    assert len(set(seen)) == len(WIZARD_STEPS)
    for out_of_order in ("verify", "plan", "bogus"):
        with pytest.raises(AdminConsoleRefused) as order:
            advance_wizard(advance_wizard(None, "plan"), out_of_order)
        assert order.value.code == "STEP_OUT_OF_ORDER"
    with pytest.raises(AdminConsoleRefused) as first:
        advance_wizard(None, "transition")
    assert first.value.code == "STEP_OUT_OF_ORDER"

    # The transition needs a link-migration dry run with zero ownership changes.
    dry = advance_wizard(
        advance_wizard(advance_wizard(None, "plan"), "backup_confirmed"),
        "link_migration_dry_run",
        ownership_changes=2,
    )
    with pytest.raises(AdminConsoleRefused) as owned:
        advance_wizard(dry, "transition")
    assert owned.value.code == "OWNERSHIP_CHANGES_PRESENT"
