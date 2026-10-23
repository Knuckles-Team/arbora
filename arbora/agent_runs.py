"""Arbora bindings for the agent-utilities run loop.

The agent-utilities harness owns the run loop (plan, model, tools, steps,
budgets, stop rules). It asks its host for every decision through
``agent_utilities.host_ports``. This module supplies those answers:

* :func:`decide_run` is the run decision provider. The engine serves no run-plan
  decision method today, so it returns the harness's documented default plan
  (``agent_utilities.runtime.default_run_plan``) over the Arbora model registry.
* :func:`resolve_model` is the model provider. It builds an OpenAI-compatible
  model for a model id that the Arbora model registry names. Any other id
  passes through, so pydantic-ai resolves it itself.
* :func:`record_run_event` is the run event sink. It writes each run event to
  the engine as one ``RunEvent`` node through the governed typed-batch seam.

:class:`AgentRunPort` adapts ``AgentRunExecutor`` to the Arbora
``AgentExecutionPort`` contract.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

from arbora.api.contracts.agent_control_contracts import (
    AgentExecutionRequest,
    AgentExecutionResult,
)
from arbora.api.contracts.session import GraphSession

logger = logging.getLogger(__name__)

# Whole JSON, including escaping and metadata; independent of A2A's text bound.
MAX_RUN_EVENT_PAYLOAD_BYTES = 1_048_576
RUN_EVENT_PAYLOAD_ERROR_SCHEMA = "arbora-run-event-payload-error-v1"

__all__ = [
    "AgentRunPort",
    "MAX_RUN_EVENT_PAYLOAD_BYTES",
    "RUN_EVENT_PAYLOAD_ERROR_SCHEMA",
    "bind_run_ports",
    "decide_run",
    "record_run_event",
    "resolve_model",
    "run_config",
]


@dataclass(frozen=True)
class NativeRunContext:
    """Host-only bindings, never a run request field or an execution grant.

    Publication must still verify native run-owner admission and the live fence.
    A legacy WorkItem claim alone does not establish that admission.
    """

    run_id: str
    task_id: str
    context_id: str
    work_item_id: str
    session: GraphSession = field(repr=False)
    claim: Mapping[str, Any] = field(repr=False)
    owner_graph: str | None = field(default=None, repr=False)
    hosted_invocation: Any = field(default=None, repr=False)
    planning_usage: dict[str, int] = field(default_factory=dict, repr=False)


@dataclass
class _NativeRunScope:
    ownership: NativeRunContext = field(repr=False)
    active: bool = True


_NATIVE_RUN: ContextVar[_NativeRunScope | None] = ContextVar(
    "arbora_native_run", default=None
)


def _valid_owner_graph(graph: str | None) -> bool:
    """An optional host routing binding must be exact, never normalized."""
    return graph is None or (
        isinstance(graph, str) and bool(graph) and graph == graph.strip()
    )


def _valid_native_claim(claim: Mapping[str, Any], session: GraphSession) -> bool:
    """Check the native lease holder, tenant, and positive integer fence."""
    return (
        claim.get("claimed") is True
        and claim.get("tenant") == session.tenant
        and isinstance(claim.get("lease_holder_ref"), str)
        and bool(claim["lease_holder_ref"].strip())
        and all(
            type(claim.get(key)) is int and claim[key] >= 1
            for key in ("lease_epoch", "fencing_token")
        )
    )


@contextmanager
def native_run_scope(
    *,
    run_id: str,
    task_id: str,
    context_id: str,
    work_item_id: str,
    session: GraphSession,
    claim: Mapping[str, Any],
    owner_graph: str | None = None,
    hosted_invocation: Any = None,
) -> Iterator[None]:
    """Bind only a claim obtained by the trusted A2A authority for this run."""
    if not _valid_owner_graph(owner_graph) or not _valid_native_claim(claim, session):
        raise RuntimeError("native run claim is invalid")
    if not run_id or not task_id or not work_item_id:
        raise RuntimeError("native run claim is invalid")
    scope = _NativeRunScope(
        NativeRunContext(
            run_id,
            task_id,
            context_id,
            work_item_id,
            session,
            MappingProxyType(dict(claim)),
            owner_graph,
            hosted_invocation,
        )
    )
    token = _NATIVE_RUN.set(scope)
    try:
        yield
    finally:
        # Context copies in child tasks share revocation, not an everlasting fence.
        scope.active = False
        _NATIVE_RUN.reset(token)


def current_native_run(request: Any) -> NativeRunContext | None:
    """Read the exact trusted binding; never recover authority from metadata."""
    scope = _NATIVE_RUN.get()
    if scope is None:
        return None
    if not scope.active:
        raise RuntimeError("native run scope is closed")
    owner = scope.ownership
    if getattr(request, "run_id", None) != owner.run_id:
        raise RuntimeError("native run identity does not match the host scope")
    metadata = getattr(request, "metadata", {})
    if isinstance(metadata, Mapping) and "session" in metadata:
        if metadata["session"] != owner.session:
            raise RuntimeError("native run session does not match the host scope")
    return owner


def _chat_models_registry(raw: Any) -> dict[str, Any]:
    """The registry shape of the configured ``CHAT_MODELS`` list, or ``{}``.

    ``CHAT_MODELS`` (config.json) is the deployment's model registry: a list of
    ``{"id", "base_url", "provider", ...}`` entries. The first entry is the
    default; ``api_key_env`` defaults to ``OPENAI_API_KEY``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else []
        except ValueError:
            return {}
    if not isinstance(raw, list):
        return {}
    models: dict[str, Any] = {}
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id or not entry.get("base_url"):
            continue
        models.setdefault(
            model_id,
            {
                "base_url": str(entry["base_url"]),
                "model": str(entry.get("model") or model_id),
                "api_key_env": str(entry.get("api_key_env") or "OPENAI_API_KEY"),
            },
        )
    if not models:
        return {}
    return {"default": next(iter(models)), "models": models}


def _registry() -> dict[str, Any]:
    """The model registry: ``MODEL_REGISTRY_PATH``, else ``CHAT_MODELS``, else ``{}``.

    Shape: ``{"default": "<model id>", "models": {"<model id>": {"base_url":
    ..., "model": ..., "api_key_env": ...}}}`` in JSON or YAML.
    """
    from arbora.core.settings import runtime_config

    path = str(runtime_config.model_registry_path or "").strip()
    if not path:
        return _chat_models_registry(getattr(runtime_config, "chat_models", None))
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
    from agent_utilities.runtime import default_run_plan

    owner = current_native_run(request)
    plan = default_run_plan(request, run_config())
    if owner is not None and owner.hosted_invocation is not None:
        from arbora.mcp_server.task_plan_runtime import admitted_run_plan

        return admitted_run_plan(request, plan, owner)
    return plan


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


def _run_event_payload(
    event: Mapping[str, Any], run_id: str, event_type: str, step: int
) -> str:
    """Serialize a bounded event or preserve its identity in an error marker."""
    payload_error = None
    try:
        payload = json.dumps(
            dict(event), sort_keys=True, default=str, ensure_ascii=True, allow_nan=False
        )
    except Exception:  # noqa: BLE001 - retain the event identity when its body fails
        payload_error = "payload_serialization_failed"
    else:
        if len(payload) > MAX_RUN_EVENT_PAYLOAD_BYTES:
            # ensure_ascii=True makes every character exactly one UTF-8 byte.
            payload_error = "payload_too_large"
    if payload_error is not None:
        marker = {
            "schema": RUN_EVENT_PAYLOAD_ERROR_SCHEMA,
            "run_id": run_id,
            "type": event_type,
            "step": step,
            "payload_error": {
                "code": payload_error,
                "max_bytes": MAX_RUN_EVENT_PAYLOAD_BYTES,
            },
        }
        status = event.get("status")
        if isinstance(status, str) and status in (
            "completed",
            "stopped",
            "failed",
            "running",
        ):
            marker["status"] = status
        payload = json.dumps(marker, sort_keys=True, ensure_ascii=True)
        logger.warning("run event body unavailable: %s", payload_error)
    return payload


def _valid_event_identity(run_id: Any, event_type: Any, step: Any) -> bool:
    """Keep event identifiers bounded before persistence or marker encoding."""
    return (
        isinstance(run_id, str)
        and 0 < len(run_id) <= 512
        and isinstance(event_type, str)
        and 0 < len(event_type) <= 64
        and type(step) is int
        and 0 <= step <= 2**63 - 1
    )


def _ordered_event(event_id: Any, sequence: Any) -> bool:
    """Only bounded event identities qualify for deterministic persistence."""
    return (
        isinstance(event_id, str)
        and 0 < len(event_id) <= 1024
        and type(sequence) is int
        and 0 < sequence <= 2**63 - 1
    )


def _event_suffix(event_id: Any, ordered: bool) -> str:
    """Keep legacy unsequenced events distinct, ordered retries idempotent."""
    if ordered and isinstance(event_id, str):
        return hashlib.sha256(event_id.encode()).hexdigest()
    return uuid.uuid4().hex


def record_run_event(event: Mapping[str, Any]) -> None:
    """Write one run event to the engine as a ``RunEvent`` node.

    The served engine creates the node through EG ``CreateNodeIfAbsent``;
    an engine with only the typed-batch seam or ``add_node`` uses that. A failed write is logged, never raised:
    a run does not fail because its trace could not be stored.
    """
    run_id = event.get("run_id")
    event_type = event.get("type")
    step = event.get("step", 0)
    if not _valid_event_identity(run_id, event_type, step):
        logger.warning("run event not persisted: invalid event identity")
        return
    payload = _run_event_payload(event, cast(str, run_id), cast(str, event_type), step)
    engine = _engine()
    event_id = event.get("event_id")
    sequence = event.get("sequence")
    ordered = _ordered_event(event_id, sequence)
    suffix = _event_suffix(event_id, ordered)
    node_id = f"runevent:{run_id}:{suffix}"
    properties = {
        "run_id": run_id,
        "task_id": event.get("task_id", run_id),
        "event_type": event_type,
        "step": step,
        "payload": payload,
    }
    if ordered:
        properties["sequence"] = sequence
        properties["event_id"] = event_id
    batch = getattr(engine, "batch_typed_mutations", None)
    create_node = getattr(engine, "create_node", None)
    add_node = getattr(engine, "add_node", None)
    try:
        if callable(create_node):
            # A RunEvent id is unique, so it is a create. The served engine's
            # AddNode is refused on a cold graph write path (EG 2.27.2).
            create_node(node_id, "RunEvent", properties)
        elif callable(batch):
            batch(
                [
                    {
                        "kind": "node",
                        "id": node_id,
                        "node_type": "RunEvent",
                        "properties": properties,
                    }
                ]
            )
        elif callable(add_node):
            add_node(node_id, "RunEvent", properties)
        else:
            logger.warning("run event not persisted: no engine is bound")
    except Exception as exc:  # noqa: BLE001 - the trace never fails the run
        # The engine's refusal code and detail are public contract values;
        # they name why the write was refused, never a payload value.
        logger.warning(
            "run event %s of run %s not persisted: %s %s %s",
            properties["event_type"],
            run_id,
            type(exc).__name__,
            getattr(exc, "code", ""),
            getattr(exc, "detail", ""),
        )


def failed_run_usage(error: Exception, run_id: str) -> dict[str, int] | None:
    """Project only AU's validated failed receipt, never exception-shaped data."""
    from agent_utilities import runtime

    failure_type = getattr(runtime, "RunFailed", None)
    if failure_type is None or type(error) is not failure_type:
        return None
    try:
        result = cast(Any, failure_type)(cast(Any, error).result).result
        if result.run_id != run_id or result.status != "failed":
            return None
        # Revalidate the public snapshot and enforce the narrower wire bounds.
        receipt = AgentExecutionResult(
            run_id=result.run_id, output="", usage=dict(result.usage)
        )
        return cast(dict[str, int] | None, receipt.usage)
    except (AttributeError, TypeError, ValueError):
        return None


class AgentRunPort:
    """``AgentExecutionPort`` over the harness ``AgentRunExecutor``."""

    def __init__(self, executor: Any = None) -> None:
        if executor is None:
            from agent_utilities.runtime import AgentRunExecutor

            executor = AgentRunExecutor(run_config())
        self._executor = executor

    async def execute_agent(
        self, request: AgentExecutionRequest, *, session: GraphSession
    ) -> AgentExecutionResult:
        ownership = current_native_run(request)
        if ownership is not None and ownership.session != session:
            raise RuntimeError("native run session does not match the host scope")
        result = await self._executor.execute_agent(request, session=session)
        return AgentExecutionResult(
            run_id=result.run_id,
            output=result.output,
            execution_mode=result.execution_mode,
            usage=getattr(result, "usage", None),
        )


def bind_run_ports() -> None:
    """Register the run decision, model and run event providers with the harness."""
    from agent_utilities import host_ports

    host_ports.register_run_decision_provider(decide_run)
    host_ports.register_model_provider(resolve_model)
    host_ports.register_run_event_sink(record_run_event)
    from arbora.current_plan_publication import publication_scope

    host_ports.register_optional_scaffold_publication_factory(publication_scope)
