"""Served-path regressions found by the arbora 0.1.3 live acceptance run."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.mcp_server.fleet_composition import LateFleetGateway


class _RecordingGateway:
    """Records the ambient session each fleet port runs under."""

    def __init__(self) -> None:
        self.seen: list[Any] = []

    def _record(self) -> Any:
        from arbora.api.contracts.session import current_session

        self.seen.append(current_session())
        return {"items": []}

    async def search(self, caller: Any, **params: Any) -> Any:
        return self._record()

    async def effect(self, op: Any, params: Any, caller: Any) -> Any:
        return self._record()

    async def call(self, caller: Any, *args: Any, **kwargs: Any) -> Any:
        return self._record()


def test_http_fleet_ports_run_under_the_callers_own_session(monkeypatch) -> None:
    """``/api/v1/ops/fleet.*`` binds no ambient session; the gateway binds the caller's.

    0.1.3: fleet.catalog.search answered INTERNAL and fleet.call UNAVAILABLE on
    the HTTP operation API because the catalog read raised SessionRequiredError.
    """
    import arbora.api.contracts.session as session_mod

    bound: list[Any] = []

    class _Use:
        def __init__(self, session: Any) -> None:
            self.session = session

        def __enter__(self) -> Any:
            bound.append(self.session)
            self.token = session_mod._current.set(self.session)
            return self.session

        def __exit__(self, *exc: Any) -> None:
            session_mod._current.reset(self.token)

    monkeypatch.setattr(session_mod, "use_session", _Use)
    late = LateFleetGateway()
    inner = _RecordingGateway()
    late.bind(inner, ops=None)
    caller_session = object()
    caller = SimpleNamespace(session=caller_session)

    asyncio.run(late.search(caller, query="x"))
    asyncio.run(late.effect(None, {}, caller))
    asyncio.run(late.call(caller, "searxng-mcp", "web_search", {}))

    assert inner.seen == [caller_session] * 3
    assert bound == [caller_session] * 3


def test_a_parameterless_eg_method_accepts_an_empty_input() -> None:
    """0.1.3: ``ask eg.graph.NodeCount`` with ``{}`` was INVALID_ARGUMENT."""
    from arbora.api.registry import EgSchemaRef
    from arbora.mcp_server.served_invocation import validate_provider_schema

    ref = EgSchemaRef(path="contract/schemas/method.request.json#/methods/NodeCount")
    assert validate_provider_schema(ref, {}) == {}
    with pytest.raises(ValueError):
        validate_provider_schema(ref, {"graph": "__commons__"})


def test_a2a_message_without_agent_name_runs_on_the_fleet_default_agent() -> None:
    """0.1.3: A2A message/send without metadata.agent_name answered 503."""
    from arbora.a2a.models import A2AMessage
    from arbora.a2a.routing import OrchestratorA2ARouter
    from arbora.host_bindings import FLEET_AGENT_NAME

    message = A2AMessage.model_validate(
        {
            "role": "user",
            "messageId": "m1",
            "kind": "message",
            "parts": [{"kind": "text", "text": "hi"}],
        }
    )
    decision = asyncio.run(
        OrchestratorA2ARouter(lambda: None).route(message, context_budget_tokens=None)
    )
    assert decision.agent_name == FLEET_AGENT_NAME
    assert decision.selection_mode == "default-agent"


def test_run_config_uses_the_configured_chat_models_registry(monkeypatch) -> None:
    """0.1.3: no MODEL_REGISTRY_PATH meant no model, so every agent run failed closed."""
    from arbora import agent_runs
    from arbora.core import settings

    models = [
        {
            "id": "qwen/qwen3.8-27b",
            "base_url": "http://vllm.example/v1",
            "provider": "openai",
        }
    ]

    class _Config:
        model_registry_path = None
        chat_models = json.dumps(models)

    monkeypatch.setattr(settings, "runtime_config", _Config())
    monkeypatch.setenv("OPENAI_API_KEY", "test-only")
    config = agent_runs.run_config()
    assert config["agent_model"] == "qwen/qwen3.8-27b"
    model = agent_runs.resolve_model("qwen/qwen3.8-27b")
    assert not isinstance(model, str)


def test_served_engine_exposes_blocking_work_items() -> None:
    """0.1.3: A2A tasks failed with 'the engine client has no work_items namespace'."""
    from arbora.mcp_server.served_engine import ServedEngine

    calls: list[Any] = []

    class _Pool:
        async def connected_client(self, context: Any, graph: str) -> Any:
            raise AssertionError("not reached")

    engine = ServedEngine(_Pool())
    namespace = engine.work_items
    assert callable(namespace.submit)
    assert calls == []
