"""Per-model concurrency applies the engine decision (ARBORA-CAPACITY-R752)."""

from __future__ import annotations

import json
from typing import Any

import pytest

from arbora.api.domain.fleet.throttle_service import (
    FALLBACK_MODEL_LIMIT,
    ModelCapacityGate,
)
from arbora.fleet.capacity_binding import model_capacity_ask, with_capacity

ANSWER = {
    "limit": 6,
    "effective_limit": 3,
    "breaker": "half_open",
    "granted": 2,
    "group_slots_free": 1,
}
PIN = {
    "component_id": "capacity",
    "kind": "feature_schema",
    "definition_digest": "sha256:" + "0" * 64,
}


def _record(option: str) -> dict[str, Any]:
    return {
        "records": [
            {"outcome": {"outcome": "advisory", "scores": [{"option_id": option}]}}
        ]
    }


@pytest.mark.spec("ARBORA-CAPACITY-R752")
async def test_limit_comes_from_engine_decision() -> None:
    asked: list[dict[str, Any]] = []

    async def decide(request: dict[str, Any]) -> Any:
        asked.append(request)
        return _record("capacity:" + json.dumps(ANSWER))

    async def observations(endpoint: str):
        return [{"at_ms": 1, "latency_ms": 900, "outcome": "success"}], {"group": "gpu"}

    gate = ModelCapacityGate(
        model_capacity_ask(decide, observations, tenant="t1", feature_schema=PIN)
    )
    answered = await gate.limit_for("model-a")
    assert (answered.limit, answered.breaker, answered.group_slots) == (
        3,
        "half_open",
        2,
    )
    (request,) = asked
    assert request["question"]["kind"] == "model_capacity"
    names = {p["name"] for p in request["params"]}
    assert names == {"capacity_observations", "capacity_bounds"}


@pytest.mark.spec("ARBORA-CAPACITY-R752")
async def test_no_decision_a_failed_ask_or_a_malformed_answer_fall_back_to_one() -> (
    None
):
    async def nothing(endpoint: str):
        return None

    async def broken(endpoint: str):
        raise RuntimeError("engine down")

    async def malformed(endpoint: str):
        return {"effective_limit": "many", "breaker": "ajar", "granted": 1}

    for ask in (nothing, broken, malformed):
        answered = await ModelCapacityGate(ask).limit_for("model-a")
        assert answered.limit == FALLBACK_MODEL_LIMIT == 1
        assert answered.breaker == "closed"


@pytest.mark.spec("ARBORA-CAPACITY-R752")
async def test_the_composition_binds_a_gate_that_falls_back_until_an_ask_is_bound() -> (
    None
):
    async def send(method: str, params: dict[str, Any], tenant: str) -> Any:
        raise AssertionError("no engine call before a window closes")

    bound, _ = with_capacity({}, send)
    assert (await bound["model_capacity_gate"].limit_for("m")).limit == 1

    async def ask(endpoint: str):
        return ANSWER

    bound, _ = with_capacity({"model_capacity_ask": ask}, send)
    assert (await bound["model_capacity_gate"].limit_for("m")).limit == 3
    assert {"throttle_registry", "outcome_recorder", "partition_limiter"} <= set(bound)
