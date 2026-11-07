"""Each connector-pack refusal cause carries its own text (ARBORA-FLEET-R015.5).

A spent budget, an unreadable ledger and a failed audit write must never share
one opaque refusal: the operator reads the cause from the refusal and the log.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import pytest

from arbora.access.action_policy import (
    DENIAL_AUDIT_UNRECORDED,
    DENIAL_LEDGER_UNREADABLE,
    DENIAL_RATE_LIMITED,
    REASON_AUDIT_UNRECORDED,
    ActionPolicy,
    ActionRequest,
)
from arbora.api.contracts.provisioning import (
    ProvisioningAuthorityError,
    _require_receipt,
)

pytestmark = pytest.mark.spec("ARBORA-FLEET-R015.5")

_POLICY = (
    "version: 1\n"
    "defaults: {tier: approval_required}\n"
    "rules:\n"
    "  - {kind: connector_pack_import, target: '*', tier: auto_notify,"
    " rate_limit: {max: 6, window_s: 3600}}\n"
)
_SECRET_PARAM = (
    "do-not-log-this-param"  # sanitizer:ignore - synthetic marker, not a credential
)


class _Ledger:
    """Answers ledger reads from scripted rows; audit writes may fail."""

    def __init__(
        self, unlabeled: int, rows: list[dict[str, Any]], *, audit_fails: bool = False
    ) -> None:
        self.unlabeled, self.rows, self.audit_fails = unlabeled, rows, audit_fails

    def query_cypher(self, query: str, params: Any = None) -> list[Any]:
        if "count(d)" in query:
            return [{"n": self.unlabeled}]
        if "governance_rule" in query:
            return []
        return self.rows

    def create_node(self, *args: Any) -> bool:
        if self.audit_fails:
            raise ConnectionError("engine write refused")
        return True


def _request() -> ActionRequest:
    return ActionRequest(
        kind="connector_pack_import",
        target="tm",
        source="fleet",
        params={"token": _SECRET_PARAM},
    )


def _refusal(policy: ActionPolicy) -> str:
    with pytest.raises(ProvisioningAuthorityError) as caught:
        _require_receipt(policy, _request())
    return str(caught.value)


def test_three_refusal_causes_have_distinct_text(tmp_path, caplog):
    policy_file = tmp_path / "policy.yml"
    policy_file.write_text(_POLICY)
    spent = [
        {"id": str(i), "target": "tm", "decision": "allow_notify", "ts": time.time()}
        for i in range(6)
    ]

    limited = ActionPolicy(_Ledger(0, spent), policy_path=policy_file)
    assert limited.decide(_request()).denial == DENIAL_RATE_LIMITED
    unreadable = ActionPolicy(_Ledger(273, []), policy_path=policy_file)
    assert unreadable.decide(_request()).denial == DENIAL_LEDGER_UNREADABLE
    unrecorded = ActionPolicy(_Ledger(0, [], audit_fails=True), policy_path=policy_file)
    decision = unrecorded.decide(_request())
    assert not decision.allowed
    assert decision.denial == DENIAL_AUDIT_UNRECORDED
    assert decision.reason == REASON_AUDIT_UNRECORDED

    texts = [_refusal(limited), _refusal(unreadable)]
    with caplog.at_level(logging.WARNING, logger="arbora.access.action_policy"):
        texts.append(_refusal(unrecorded))
    assert "rate limit exceeded" in texts[0]
    assert "ledger unreadable: 273 rows lack label ActionDecision" in texts[1]
    assert texts[2].endswith(REASON_AUDIT_UNRECORDED)
    assert "rate limit" not in texts[2]
    assert len(set(texts)) == 3

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    audit_logs = [
        r.getMessage() for r in warnings if "audit write failed" in r.getMessage()
    ]
    assert audit_logs
    assert "ConnectionError" in audit_logs[0]
    assert "kind=connector_pack_import target=tm" in audit_logs[0]
    assert all(_SECRET_PARAM not in r.getMessage() for r in caplog.records)
