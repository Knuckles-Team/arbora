"""Provisioning contracts (ARBORA-FLEET-R024.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.contracts.provisioning_capacity import (
    AggregationRef,
    FleetCapacityProof,
    ProvisionRequest,
    ProvisionResult,
)
from tests.fleet._provisioning_support import IMAGE, request


@pytest.mark.spec("ARBORA-FLEET-R024.4")
def test_request_result_round_trip() -> None:
    req = request("k", 4)
    assert ProvisionRequest.model_validate(req.model_dump()) == req
    result = ProvisionResult(
        instance_ids=("i0", "i1"),
        allocation_receipt_digest="b" * 64,
        outcomes={"i0": "started", "i1": "failed"},
        shortfall=2,
    )
    assert ProvisionResult.model_validate(result.model_dump()) == result
    proof = FleetCapacityProof(
        requested_count=4,
        admitted_ids=("i0",),
        started_ids=("i0",),
        completed_ids=(),
        failed_ids=(),
        peak_concurrency=4,
        budget_consumed=1,
        started_at=1,
        finished_at=2,
        receipt_digests=("b" * 64,),
        aggregation=AggregationRef(
            operation="agg", version="1", input_receipt_ids=("r",)
        ),
    )
    assert FleetCapacityProof.model_validate(proof.model_dump()) == proof


@pytest.mark.spec("ARBORA-FLEET-R024.4")
def test_missing_binding_field_and_bad_digest_refuse() -> None:
    body = request("k").model_dump()
    del body["idempotency_key"]
    with pytest.raises(ValidationError):
        ProvisionRequest.model_validate(body)
    with pytest.raises(ValidationError):
        ProvisionRequest.model_validate(
            {**request("k").model_dump(), "image_digest": "x"}
        )
    with pytest.raises(ValidationError):
        ProvisionRequest.model_validate({**request("k").model_dump(), "extra": 1})
    assert request("k").image_digest == IMAGE
