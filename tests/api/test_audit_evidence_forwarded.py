"""The audit adapter forwards screening and hook evidence (ARBORA-OPS-R042.5, R009.9)."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from test_invoke_contract import caller

from arbora.api.invoke.eg_audit import EgAuditAdapter
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.registry import AuditClass

EVENT = {
    "tenant": "tenant:one",
    "principal": "person:one",
    "plan_digest": "b" * 64,
    "op": "example.write",
    "surface": "http",
    "request_id": "request:one",
    "screen_verdict": "flagged",
    "finding_classes": "perception",
    "rule_ids": "screen.perception.hidden_text",
    "redaction_count": "1",
    "hooks": "pre:allow;post:allow|x" + "y" * 300,
}


def _adapter(append: Any) -> EgAuditAdapter:
    @contextmanager
    def context(_claims: Any):
        yield

    async def client(_tenant: str) -> Any:
        return SimpleNamespace(
            admin=SimpleNamespace(audit_append=append), use_verified_context=context
        )

    return EgAuditAdapter(client)


def _receipt(status: str) -> dict[str, Any]:
    return {
        "graph": "tenant:one",
        "seq": 7 if status == "reserved" else 8,
        "entry_sha256": "a" * 64,
        "replayed": False,
        "reservation_seq": 7,
        "outcome_seq": None if status == "reserved" else 8,
    }


@pytest.mark.spec("ARBORA-OPS-R042.5", "ARBORA-OPS-R009.9")
def test_evidence_is_forwarded_in_the_engine_bounds() -> None:
    seen: list[dict[str, Any]] = []

    async def append(
        *, op, surface, params_sha256, status, request_id, audit_class, evidence=None
    ):
        seen.append({"status": status, "evidence": evidence})
        return _receipt(status)

    asyncio.run(_adapter(append).preflight(EVENT, AuditClass.EVENT, caller()))
    evidence = seen[0]["evidence"]
    assert set(evidence) == set(EgAuditAdapter.EVIDENCE_KEYS)
    assert (
        evidence["screen_verdict"] == "flagged" and evidence["redaction_count"] == "1"
    )
    hooks = evidence["hooks"]
    assert "|" not in hooks and ";" not in hooks and len(hooks.encode()) <= 256
    assert len(evidence) <= 16


@pytest.mark.spec("ARBORA-OPS-R042.5", "ARBORA-OPS-R009.9")
def test_a_pin_without_the_evidence_field_refuses_when_evidence_is_supplied() -> None:
    seen: list[str] = []

    async def append(*, op, surface, params_sha256, status, request_id, audit_class):
        seen.append(status)
        return _receipt(status)

    with pytest.raises(OperationRefused) as refused:
        asyncio.run(_adapter(append).preflight(EVENT, AuditClass.EVENT, caller()))
    assert refused.value.code == "UNAVAILABLE"
    assert "AuditAppend.evidence" in str(refused.value.details)
    assert seen == []


@pytest.mark.spec("ARBORA-OPS-R042.5", "ARBORA-OPS-R009.9")
def test_a_pin_without_the_evidence_field_still_writes_a_bare_event() -> None:
    seen: list[str] = []

    async def append(*, op, surface, params_sha256, status, request_id, audit_class):
        seen.append(status)
        return _receipt(status)

    bare = {k: v for k, v in EVENT.items() if k not in EgAuditAdapter.EVIDENCE_KEYS}
    asyncio.run(_adapter(append).preflight(bare, AuditClass.EVENT, caller()))
    assert seen == ["reserved"]


@pytest.mark.spec("ARBORA-OPS-R042.5", "ARBORA-OPS-R009.9")
def test_an_event_with_no_evidence_forwards_none() -> None:
    seen: list[Any] = []

    async def append(
        *, op, surface, params_sha256, status, request_id, audit_class, evidence=None
    ):
        seen.append(evidence)
        return _receipt(status)

    bare = {k: v for k, v in EVENT.items() if k not in EgAuditAdapter.EVIDENCE_KEYS}
    asyncio.run(_adapter(append).preflight(bare, AuditClass.EVENT, caller()))
    assert seen == [None]
