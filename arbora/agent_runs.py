"""Arbora bindings for the agent-utilities run loop.

The agent-utilities harness owns the run loop (plan, model, tools, steps,
budgets, stop rules). It asks its host for every decision through
``agent_utilities.host_ports``. This module supplies those answers:

* :func:`decide_run` is the run decision provider. The engine serves no run-plan
  decision method today, so it returns the harness's documented default plan
  (``agent_utilities.graph.default_run_plan``) over the Arbora model registry.
* :func:`resolve_model` is the model provider. It builds an OpenAI-compatible
  model for a model id that the Arbora model registry names. Any other id
  passes through, so pydantic-ai resolves it itself.
* :func:`record_run_event` is the run event sink. It writes each run event to
  the engine as one ``RunEvent`` node through the governed typed-batch seam.

:class:`AgentRunPort` adapts ``AgentRunExecutor`` to the Arbora
``AgentExecutionPort`` contract.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from arbora.api.contracts.agent_control_contracts import (
    AgentExecutionRequest,
    AgentExecutionResult,
)
from arbora.api.contracts.session import GraphSession

logger = logging.getLogger(__name__)

__all__ = [
    "AgentRunPort",
    "bind_run_ports",
    "decide_run",
    "record_run_event",
    "resolve_model",
    "run_config",
]


def _registry() -> dict[str, Any]:
    """The model registry document named by ``MODEL_REGISTRY_PATH``, or ``{}``.

    Shape: ``{"default": "<model id>", "models": {"<model id>": {"base_url":
    ..., "model": ..., "api_key_env": ...}}}`` in JSON or YAML.
    """
    from arbora.core.settings import runtime_config

    path = str(runtime_config.model_registry_path or "").strip()
    if not path:
        return {}
    text = Path(path).expanduser().read_text(encoding="utf-8")
    if path.endswith((".yaml", ".yml")):
        import yaml

        document = yaml.safe_load(text)
    else:
        document = json.loads(text)
    return document if isinstance(document, dict) else {}


def run_config() -> dict[str, Any]:
    """Harness config for one run: the registry's default model, when named."""
    default = str(_registry().get("default") or "").strip()
    return {"agent_model": default} if default else {}


def decide_run(request: Any) -> Any:
    """The run plan for ``request``.

    The engine serves no run-plan decision method, so this returns the
    harness's documented default plan. A run without a model fails closed.
    """
    from agent_utilities.graph import default_run_plan

    return default_run_plan(request, run_config())


def resolve_model(model_id: str) -> Any:
    """The model for ``model_id``: a registry endpoint, else the id itself."""
    entry = (_registry().get("models") or {}).get(model_id)
    if not isinstance(entry, Mapping) or not entry.get("base_url"):
        return model_id
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    api_key_env = str(entry.get("api_key_env") or "")
    provider = OpenAIProvider(
        base_url=str(entry["base_url"]),
        api_key=os.environ.get(api_key_env, "") if api_key_env else None,
    )
    return OpenAIChatModel(str(entry.get("model") or model_id), provider=provider)


def _engine() -> Any:
    from arbora.mcp_server import runtime

    get_engine = runtime._get_engine
    if not callable(get_engine):
        return None
    try:
        return get_engine()
    except Exception as exc:  # noqa: BLE001 - an unserved engine leaves events unpersisted
        logger.warning("run events not persisted: engine unavailable (%s)", exc)
        return None


def record_run_event(event: Mapping[str, Any]) -> None:
    """Write one run event to the engine; without an engine it is logged only."""
    engine = _engine()
    write = getattr(engine, "batch_typed_mutations", None)
    if not callable(write):
        logger.debug("run event not persisted: no engine is bound")
        return
    run_id = str(event.get("run_id") or "")
    properties = {
        "run_id": run_id,
        "event_type": str(event.get("type") or ""),
        "step": int(event.get("step") or 0),
        "payload": json.dumps(dict(event), sort_keys=True, default=str),
    }
    write(
        [
            {
                "kind": "node",
                "id": f"runevent:{run_id}:{uuid.uuid4().hex}",
                "node_type": "RunEvent",
                "properties": properties,
            }
        ]
    )


class AgentRunPort:
    """``AgentExecutionPort`` over the harness ``AgentRunExecutor``."""

    def __init__(self, executor: Any = None) -> None:
        if executor is None:
            from agent_utilities.graph import AgentRunExecutor

            executor = AgentRunExecutor(run_config())
        self._executor = executor

    async def execute_agent(
        self, request: AgentExecutionRequest, *, session: GraphSession
    ) -> AgentExecutionResult:
        result = await self._executor.execute_agent(request, session=session)
        return AgentExecutionResult(
            run_id=result.run_id,
            output=result.output,
            execution_mode=result.execution_mode,
        )


def bind_run_ports() -> None:
    """Register the run decision, model and run event providers with the harness."""
    from agent_utilities import host_ports

    host_ports.register_run_decision_provider(decide_run)
    host_ports.register_model_provider(resolve_model)
    host_ports.register_run_event_sink(record_run_event)
