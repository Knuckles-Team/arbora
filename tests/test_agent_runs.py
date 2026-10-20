"""Agent runs execute through the harness run loop with the Arbora providers bound."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from agent_utilities import host_ports

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


@pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")
@pytest.mark.parametrize("available", [False, True])
def test_publication_registration_requires_optional_host_contract(
    monkeypatch, available
):
    from arbora.current_plan_publication import publication_scope

    registered = []
    if available:
        monkeypatch.setattr(
            host_ports,
            "register_optional_scaffold_publication_factory",
            registered.append,
            raising=False,
        )
    else:
        monkeypatch.delattr(
            host_ports, "register_optional_scaffold_publication_factory", raising=False
        )
    agent_runs.bind_run_ports()
    assert registered == ([publication_scope] if available else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "stopped", "running"])
async def test_incomplete_run_result_never_projects_success(status):
    from types import SimpleNamespace

    async def execute(*args, **kwargs):
        return SimpleNamespace(status=status, error="sensitive provider detail")

    port = agent_runs.AgentRunPort(executor=SimpleNamespace(execute_agent=execute))
    with pytest.raises(RuntimeError, match="^agent run did not complete$"):
        await port.execute_agent(
            AgentExecutionRequest(agent_name="helper", task="work"),
            session=cast(Any, object()),
        )
