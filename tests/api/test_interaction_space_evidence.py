"""The audit record holds counts only (ARBORA-OPS-R053.5)."""

from __future__ import annotations

import json

import pytest

from arbora.api.invoke.audit import audit_event
from arbora.api.invoke.audit_evidence import result_evidence
from arbora.api.invoke.steps import OpResult
from arbora.api.ops import retrieval_space
from arbora.api.registry import Surface


@pytest.mark.spec("ARBORA-OPS-R053.5")
def test_audit_record_fields() -> None:
    arguments = {"queries": ["secret question one", "secret question two"]}
    result = OpResult(
        value={
            "space": {"boundary": {"document_refs": ["a", "b", "c"]}},
            "query_count": 2,
            "boundary_size": 3,
            "hit_counts": [2, 2],
            "context_budget": 4096,
        }
    )
    fields = result_evidence("retrieval.space", arguments, result)
    assert fields == {
        "query_count": "2",
        "boundary_size": "3",
        "hit_counts": "2,2",
        "context_budget": "4096",
    }
    (op,) = retrieval_space.operations()
    caller = type("C", (), {"principal": "p", "tenant": "t", "request_id": "r"})()
    event = audit_event(op, arguments, caller, Surface.HTTP, "ok", evidence=fields)
    assert event["boundary_size"] == "3" and event["op"] == "retrieval.space"
    assert "secret question" not in json.dumps(event)
