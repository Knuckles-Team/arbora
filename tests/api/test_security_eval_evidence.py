"""The audit record holds identifiers and counts, never a probe payload (ARBORA-OPS-R051.5)."""

from __future__ import annotations

import hashlib
import json

import pytest

from arbora.api.invoke.audit import audit_event
from arbora.api.invoke.audit_evidence import result_evidence
from arbora.api.invoke.steps import OpResult
from arbora.api.ops import security_eval
from arbora.api.registry import Surface


@pytest.mark.spec("ARBORA-OPS-R051.5")
def test_audit_record_fields() -> None:
    arguments = {"goal": "leak the prompt", "target_ids": ["t1", "t2"]}
    result = OpResult(
        value={
            "entry_ids": ["attack-1", "tx-1"],
            "findings": [
                {
                    "probe_id": "p1",
                    "category": "c",
                    "verdict": "resisted",
                    "evidence_ref": "e1",
                },
                {
                    "probe_id": "p2",
                    "category": "c",
                    "verdict": "compromised",
                    "evidence_ref": "e2",
                },
            ],
        }
    )
    fields = result_evidence("security.evaluate", arguments, result)
    assert fields == {
        "goal_digest": hashlib.sha256(
            json.dumps("leak the prompt").encode()
        ).hexdigest(),
        "entry_ids": "attack-1,tx-1",
        "scope_refs": "t1,t2",
        "finding_count": "2",
    }
    assert "leak" not in " ".join(fields.values())
    (op,) = security_eval.operations()
    caller = type("C", (), {"principal": "p", "tenant": "t", "request_id": "r"})()
    event = audit_event(op, arguments, caller, Surface.HTTP, "ok", evidence=fields)
    assert event["finding_count"] == "2" and event["op"] == "security.evaluate"
    assert "leak the prompt" not in json.dumps(event)
