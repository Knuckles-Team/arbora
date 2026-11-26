"""A capacity adjustment actuates once on the engine's allow (ARBORA-DEPLOY-R017.17)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import pytest

from tests.api._loopback_engine import PRINCIPAL, loopback  # noqa: F401

OP = "capacity.adjust.apply"
DIGEST = "a" * 64
PROPOSAL = {
    "proposal_id": "p1",
    "tenant_id": "tenant:one",
    "resource_class_iri": "cell:workers",
    "unit": "slots",
    "current_capacity": 4,
    "proposed_capacity": 6,
    "premise_revision": 9,
    "policy_revision": "pol-1",
    "proposal_digest": DIGEST,
}


def _record(request: dict[str, Any], answer: str, echoed: dict[str, Any]) -> Any:
    params = [
        {**p, "value": {**p["value"], "value": json.dumps(echoed, sort_keys=True)}}
        if p["name"] == "proposal"
        else p
        for p in request["params"]
    ]
    return {
        "records": [
            {
                "caller_principal": "principal:sha256:"
                + hashlib.sha256(PRINCIPAL.encode()).hexdigest(),
                "tenant_id": request["tenant_id"],
                "question": request["question"],
                "inputs": {
                    "params": request["params"] if echoed is PROPOSAL else params
                },
                "record_id": "decision:cap",
                "record_digest": "digest:cap",
                "outcome": {"answer": answer},
            }
        ]
    }


def _stack(loop: Any, answer: str, echoed: dict[str, Any] = PROPOSAL) -> None:
    from arbora.api.ops import _engine

    loop.config["tools"] = {"capacity_scale": {"server": "ops", "tool": "scale"}}
    loop.serve_decide(lambda request: _record(request, answer, echoed))
    loop.serve_unreleased(
        _engine,
        {
            "CapacityStatus": lambda params, principal: {
                "cells": [{"capacity": 6}],
                "leases": [
                    {"amount": 2, "state": "active"},
                    {"amount": 5, "state": "released"},
                ],
            }
        },
    )


@pytest.mark.spec("ARBORA-DEPLOY-R017.17")
def test_allow_actuates_once_and_returns_receipt_refuse_stale_and_mismatch_do_nothing(
    loopback: Any,  # noqa: F811
) -> None:
    _stack(loopback, "allow")
    done = loopback.confirmed(OP, {"proposal": PROPOSAL}, "cap-1")

    receipt = loopback.value(done)
    assert receipt["verdict"] == "allow"
    assert receipt["observed_capacity"] == 6 and receipt["headroom"] == 4
    assert [key for _, _, key in loopback.steps.calls] == [f"capacity:{DIGEST}"]
    assert loopback.steps.calls[0][1]["proposed_capacity"] == 6

    # A refuse verdict returns the engine reason code and runs nothing.
    before = len(loopback.steps.calls)
    _stack(loopback, "refuse:CAPACITY_PREMISE_STALE")
    refused = loopback.confirmed(OP, {"proposal": PROPOSAL}, "cap-2")
    assert refused.json()["error"]["code"] == "CAPACITY_PREMISE_STALE", refused.text

    # A verdict bound to other revisions or another digest actuates nothing.
    for field, other in (("premise_revision", 8), ("proposal_digest", "b" * 64)):
        _stack(loopback, "allow", {**PROPOSAL, field: other})
        mismatch = loopback.confirmed(OP, {"proposal": PROPOSAL}, f"cap-{field}")
        assert mismatch.json()["error"]["code"] == "RECEIPT_MISMATCH", mismatch.text

    # A proposal without its bound fields never reaches the engine.
    bare = {k: v for k, v in PROPOSAL.items() if k != "proposal_digest"}
    invalid = loopback.confirmed(OP, {"proposal": bare}, "cap-bare")
    assert invalid.json()["error"]["code"] == "INVALID_ARGUMENT", invalid.text
    assert len(loopback.steps.calls) == before
