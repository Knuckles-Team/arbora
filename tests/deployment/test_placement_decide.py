"""The placement question asks the engine and passes its record through."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from typing import Any

import pytest

from arbora.api.domain.deployment.placement import ask_placement, chosen_target
from arbora.api.invoke.pipeline import OperationRefused

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R017.1")

SUBJECT = {
    "workload_iri": "workload:arbora",
    "cpu_millis": 500,
    "memory_bytes": 1 << 30,
    "replicas": 2,
    "needs_persistent_volume": False,
}
CONSTRAINTS = {
    "requires_ha": False,
    "forbidden_targets": [],
    "max_relative_cost": None,
    "capacity_cells": [],
    "priority": "orchestration",
}


def _record(request: dict[str, Any], principal: str) -> dict[str, Any]:
    return {
        "caller_principal": "principal:sha256:"
        + hashlib.sha256(principal.encode()).hexdigest(),
        "tenant_id": request["tenant_id"],
        "question": request["question"],
        "inputs": {"params": request["params"]},
        "record_id": "decision:1",
        "record_digest": "digest:1",
        "outcome": {
            "outcome": "advisory",
            "scores": [{"option_id": "placement:compose", "score": 1}],
        },
    }


def _ask(decide: Any, **overrides: Any) -> Any:
    args: dict[str, Any] = {
        "tenant": "tenant:one",
        "principal": "person:one",
        "graph": "graph:one",
        "feature_schema": {"component_id": "schema"},
        "subject": SUBJECT,
        "constraints": CONSTRAINTS,
        "candidate_targets": ["placement:compose", "placement:helm"],
    }
    return asyncio.run(ask_placement(decide, **{**args, **overrides}))


def test_request_fields_record_passthrough_and_no_backend_names() -> None:
    sent: list[dict[str, Any]] = []

    async def decide(request: dict[str, Any]) -> Any:
        sent.append(request)
        return {"records": [_record(request, "person:one")]}

    record = _ask(decide)
    request = sent[0]
    assert request["question"]["kind"] == "placement"
    body = request["params"][0]["value"]["value"]["body"]
    assert body["subject"] == SUBJECT
    assert body["constraints"] == CONSTRAINTS
    # The candidates are the caller's; none is added, dropped or ordered here.
    assert body["candidate_targets"] == ["placement:compose", "placement:helm"]
    assert record == _record(request, "person:one")
    assert chosen_target(record) == "compose"

    import arbora.api.domain.deployment.placement as module

    source = inspect.getsource(module)
    for backend in ("compose", "helm", "swarm", "kubernetes", "standalone"):
        assert backend not in source.replace("compose, helm", "")


@pytest.mark.parametrize(
    "case", ["other_principal", "other_tenant", "no_record", "two_records"]
)
def test_a_record_that_does_not_answer_the_request_is_refused(case: str) -> None:
    async def decide(request: dict[str, Any]) -> Any:
        record = _record(
            request, "person:two" if case == "other_principal" else "person:one"
        )
        if case == "other_tenant":
            record["tenant_id"] = "tenant:two"
        if case == "no_record":
            return {"records": []}
        if case == "two_records":
            return {"records": [record, record]}
        return {"records": [record]}

    with pytest.raises(OperationRefused) as refused:
        _ask(decide)
    assert refused.value.code == "RECEIPT_MISMATCH"


def test_a_body_the_engine_contract_rejects_is_refused_before_any_send() -> None:
    async def decide(request: dict[str, Any]) -> Any:
        raise AssertionError("nothing is sent")

    with pytest.raises(OperationRefused) as refused:
        _ask(decide, subject={**SUBJECT, "surprise": 1})
    assert refused.value.code == "INVALID_ARGUMENT"
