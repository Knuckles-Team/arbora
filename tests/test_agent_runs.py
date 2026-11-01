"""Agent runs execute through the harness run loop with the Arbora providers bound."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from meristem import host_ports

from arbora import agent_runs
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from arbora.core.settings import runtime_config
from arbora.mcp_server import runtime


class _Engine:
    def __init__(self) -> None:
        self.batches: list[list[dict[str, Any]]] = []

    def batch_typed_mutations(self, mutations: list[dict[str, Any]]) -> bool:
        self.batches.append(mutations)
        return True


@pytest.fixture
def bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Engine]:
    registry = tmp_path / "models.json"
    registry.write_text(json.dumps({"default": "test"}), encoding="utf-8")
    monkeypatch.setattr(runtime_config, "model_registry_path", str(registry))
    engine = _Engine()
    monkeypatch.setattr(runtime, "_get_engine", lambda: engine)
    agent_runs.bind_run_ports()
    yield engine
    host_ports.register_run_decision_provider(None)
    host_ports.register_model_provider(None)
    host_ports.register_run_event_sink(None)


@pytest.mark.spec("ARBORA-HOST-R001.2.1")
@pytest.mark.asyncio
async def test_agent_run_executes_through_the_harness_with_bound_providers(
    bound: _Engine,
) -> None:
    result = await agent_runs.AgentRunPort().execute_agent(
        AgentExecutionRequest(agent_name="helper", task="say hello"),
        session=cast(Any, object()),
    )

    assert result.run_id and result.output
    terminal = json.loads(bound.batches[-1][0]["properties"]["payload"])
    assert result.usage == terminal["result"]["usage"]
    assert result.usage and result.usage["requests"] == 1
    kinds = [
        json.loads(batch[0]["properties"]["payload"])["type"] for batch in bound.batches
    ]
    assert kinds == ["run_started", "node_transition", "graph_complete"]
    assert {batch[0]["node_type"] for batch in bound.batches} == {"RunEvent"}


@pytest.mark.spec("ARBORA-HOST-R001.2.1")
def test_a_registry_endpoint_builds_an_openai_compatible_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / "models.json"
    registry.write_text(
        json.dumps(
            {"models": {"local": {"base_url": "http://127.0.0.1:1/v1", "model": "m"}}}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(runtime_config, "model_registry_path", str(registry))

    assert agent_runs.resolve_model("unlisted") == "unlisted"
    assert agent_runs.resolve_model("local").model_name == "m"


@pytest.mark.spec("ARBORA-HOST-R001.2.1")
@pytest.mark.asyncio
async def test_a_run_without_a_model_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime_config, "model_registry_path", None)
    agent_runs.bind_run_ports()
    try:
        with pytest.raises(RuntimeError, match="no model"):
            await agent_runs.AgentRunPort().execute_agent(
                AgentExecutionRequest(agent_name="helper", task="say hello"),
                session=cast(Any, object()),
            )
    finally:
        host_ports.register_run_decision_provider(None)
        host_ports.register_model_provider(None)
        host_ports.register_run_event_sink(None)


@pytest.mark.spec("ARBORA-A2A-R001")
@pytest.mark.parametrize(
    "usage",
    [
        {"input_tokens": -1},
        {"requests": True},
        {"cost": 12},
        {"output_tokens": 1.5},
        {"input_tokens": 9_007_199_254_740_992},
    ],
)
def test_usage_contract_refuses_unmeasured_or_invalid_counts(usage: Any) -> None:
    from pydantic import ValidationError

    from arbora.api.contracts.agent_control_contracts import AgentExecutionResult

    with pytest.raises(ValidationError):
        AgentExecutionResult(run_id="run", output="ok", usage=usage)


@pytest.mark.spec("ARBORA-A2A-R001")
def test_missing_usage_is_not_a_zero_measurement() -> None:
    from arbora.api.contracts.agent_control_contracts import AgentExecutionResult

    assert AgentExecutionResult(run_id="run", output="ok").usage is None
