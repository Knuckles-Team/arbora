"""Built-in content-pack imports leave the connector budget whole (ARBORA-FLEET-R015.7).

A fresh deploy imports Arbora's first-party content packs unattended. Those
imports are pre-authorized provisioning, so they must not spend the
blast-radius target budget that fleet connector onboarding relies on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from arbora.access.action_policy import (
    BUILTIN_CONTENT_PACKS,
    DEFAULT_POLICY,
    PACK_IMPORT_KIND,
    TIER_AUTO_NOTIFY,
    ActionPolicy,
    ActionRequest,
)

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015.7")

_CONNECTORS = ("conn-a", "conn-b", "conn-c")


class _LeaseView:
    """A minimal ``action.approval`` control-lease view for filing approvals."""

    def __init__(self) -> None:
        self.tenant = "tenant-a"
        self.issued: list[dict[str, Any]] = []

    def list(
        self,
        *,
        tenant: str,
        kind: str,
        status: str,
        grant_match: dict[str, Any] | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        return {
            "leases": [
                lease
                for lease in self.issued
                if lease["kind"] == kind and lease["status"] == status
            ],
            "next_cursor": None,
        }

    def issue(
        self,
        *,
        tenant: str,
        lease_id: str,
        kind: str,
        grant: dict[str, Any],
        issued_at_ms: int,
        expires_at_ms: int,
        hard_expires_at_ms: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        lease = {
            "lease_id": lease_id,
            "kind": kind,
            "grant": grant,
            "status": "active",
            "issued_at_ms": issued_at_ms,
            "expires_at_ms": expires_at_ms,
            "hard_expires_at_ms": hard_expires_at_ms,
            "revision": 1,
        }
        self.issued.append(lease)
        return {"outcome": "issued", "lease": lease}


class _RecordingLedger:
    """Keeps every written ``ActionDecision`` row and answers ledger scans."""

    def __init__(self) -> None:
        self.decisions: list[dict[str, Any]] = []
        self.approvals: list[str] = []
        # Held decisions file their approval as a native control lease
        # (``IssueControlLease``), never as a generic node write.
        self.control_leases = _LeaseView()

    def query_cypher(self, query: str, params: Any = None) -> list[Any]:
        if "count(d)" in query:
            return [{"n": 0}]
        if "ActionDecision" in query:
            kind = (params or {}).get("kind")
            return [dict(row) for row in self.decisions if row["kind"] == kind]
        return []

    def create_node(self, node_id: str, label: str, props: dict[str, Any]) -> bool:
        if label == "ActionDecision":
            self.decisions.append(
                {
                    "id": node_id,
                    "kind": props["kind"],
                    "target": props["target"],
                    "decision": props["decision"],
                    "ts": props["decided_unix"],
                }
            )
        elif label == "ActionApproval":
            self.approvals.append(node_id)
        return True


def _policy(tmp_path, ledger: _RecordingLedger, connectors: tuple[str, ...]):
    """The shipped default plus the operator's admission of ``connectors``."""
    policy = {
        **DEFAULT_POLICY,
        "rules": [
            *(
                {"kind": PACK_IMPORT_KIND, "target": name, "tier": TIER_AUTO_NOTIFY}
                for name in connectors
            ),
            *DEFAULT_POLICY["rules"],
        ],
    }
    path = tmp_path / "action_policy.json"
    path.write_text(json.dumps(policy))
    return ActionPolicy(ledger, policy_path=path)


def _import(policy: ActionPolicy, target: str):
    return policy.decide(
        ActionRequest(kind=PACK_IMPORT_KIND, target=target, source="arbora")
    )


def test_fresh_deploy_leaves_the_full_budget_for_connectors(tmp_path):
    cap = DEFAULT_POLICY["defaults"]["blast_radius"]["max_targets"]
    assert len(_CONNECTORS) == cap
    ledger = _RecordingLedger()
    policy = _policy(tmp_path, ledger, _CONNECTORS)

    for pack in BUILTIN_CONTENT_PACKS:
        assert _import(policy, pack).allowed, pack
    for connector in _CONNECTORS:
        decision = _import(policy, connector)
        assert decision.allowed, (connector, decision.reason)

    assert ledger.approvals == []
    assert ledger.control_leases.issued == []
    assert {row["target"] for row in ledger.decisions} == {
        *BUILTIN_CONTENT_PACKS,
        *_CONNECTORS,
    }


def test_connector_imports_beyond_the_cap_are_still_held(tmp_path):
    extra = "conn-d"
    ledger = _RecordingLedger()
    policy = _policy(tmp_path, ledger, (*_CONNECTORS, extra))

    for pack in BUILTIN_CONTENT_PACKS:
        assert _import(policy, pack).allowed
    for connector in _CONNECTORS:
        assert _import(policy, connector).allowed

    held = _import(policy, extra)
    assert not held.allowed
    assert "blast-radius cap" in held.reason
    # The hold filed exactly one approval lease for the over-cap import.
    assert len(ledger.control_leases.issued) == 1
