"""Compose a model proposal with EG admission and AU's existing run loop."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import replace
from importlib import import_module
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import Agent
from pydantic_ai.usage import RunUsage, UsageLimits

from arbora.api.invoke.bootstrap import _caller_dispatch_graph
from arbora.api.invoke.executor import execution_context
from arbora.api.invoke.pipeline import OperationRefused, _authorize
from arbora.api.invoke.steps import OpError, claim_values
from arbora.api.mcp.caller import caller_from_session


class ProposedTask(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    task_id: str = Field(min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=8192)
    depends_on: list[str] = Field(default_factory=list, max_length=64)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tasks: list[ProposedTask] = Field(min_length=1, max_length=64)
    output_task_id: str = Field(min_length=1, max_length=128)


def _contracts() -> tuple[Any, Any]:
    try:
        au = import_module("agent_utilities.graph")
        eg = import_module("epistemic_graph.generated.coordination")
        models = import_module("epistemic_graph.generated.models")
        if not all(hasattr(au, name) for name in ("DependencyPlan", "RunTask")):
            raise ImportError("AU dependency runtime")
        if not all(
            name in au.RunPlan.__dataclass_fields__
            for name in ("initial_usage", "initial_elapsed_seconds")
        ):
            raise ImportError("AU initial consumption accounting")
        if (
            "task_plan" not in models.CurrentPlanSnapshot.model_fields
            or "task_plan_proposal" not in models.PublishCurrentPlanRequest.model_fields
        ):
            raise ImportError("EG executable publication")
        for name in (
            "send_publish_current_plan",
            "decode_publish_current_plan",
            "send_get_current_plan",
            "decode_get_current_plan",
        ):
            if not callable(getattr(eg, name, None)):
                raise ImportError(name)
    except (ImportError, AttributeError) as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "admitted_task_plan_runtime"}
        ) from exc
    return au, eg


async def _admit(host: Any, caller: Any, op_id: str) -> Any:
    op = host.services.registry.get(op_id)
    if op is None:
        raise OperationRefused("UPGRADE_REQUIRED", {"operation": op_id})
    verdict = await _authorize(op, {}, caller, host.services, None)
    if isinstance(verdict, OpError):
        raise OperationRefused(verdict.code, verdict.details)
    return op


async def _publish(
    host: Any, caller: Any, owner: Any, plan: Any, proposal: Any, eg: Any
) -> Any:
    publish = "eg.coordination.PublishCurrentPlan"
    read = "eg.coordination.GetCurrentPlan"
    op = await _admit(host, caller, publish)
    await _admit(host, caller, read)
    now = int(time.time() * 1000)
    snapshot = {
        "run_id": owner.run_id,
        "revision": 0,
        "observed_at_ms": now,
        "expires_at_ms": now + int(host.task_plan_config.timeout_seconds * 1000),
        "items": [],
        "task_plan": claim_values(plan),
    }
    payload = {
        "tenant": caller.tenant,
        "worker_id": owner.claim["lease_holder_ref"],
        "lease_epoch": owner.claim["lease_epoch"],
        "fencing_token": owner.claim["fencing_token"],
        "idempotency_key": f"task-plan:{owner.run_id}:{plan['plan_digest']}",
        "snapshot": snapshot,
        "task_plan_proposal": proposal,
    }
    # One immutable attempt payload. Do not regenerate timestamps on retry.
    async with execution_context(op, caller, host.services.runtime) as context:
        # Revalidate the same caller, but route to the graph that admitted the
        # native owner. The EG client retains all caller graph capability checks.
        await _caller_dispatch_graph(context)
        graph = owner.owner_graph
        if not isinstance(graph, str) or not graph or graph != graph.strip():
            raise OperationRefused("UNAVAILABLE", {"capability": "native_owner_graph"})
        existing = eg.decode_get_current_plan(
            await eg.send_get_current_plan(
                context.client, {"run_id": owner.run_id, "tenant": caller.tenant}, graph
            )
        )
        await _admit(host, caller, publish)
        snapshot["revision"] = _publication_revision(
            existing, owner.run_id, snapshot["task_plan"]
        )
        receipt = eg.decode_publish_current_plan(
            await eg.send_publish_current_plan(
                context.client, {"request": payload}, graph
            )
        )
        await _admit(host, caller, publish)
        await _admit(host, caller, read)
        if receipt.run_id != owner.run_id:
            raise OperationRefused("INVALID_TASK_PLAN_RECEIPT")
        stored = eg.decode_get_current_plan(
            await eg.send_get_current_plan(
                context.client, {"run_id": owner.run_id, "tenant": caller.tenant}, graph
            )
        )
        await _admit(host, caller, read)
        return _verified_readback(stored, owner.run_id, snapshot["task_plan"], receipt)


def _publication_revision(existing: Any, run_id: str, plan: Any) -> Any:
    """Advance scaffolds, but never reconstruct an immutable publication retry."""
    if existing is None:
        return 0
    previous = existing.model_dump(mode="json")
    if previous["run_id"] != run_id:
        raise OperationRefused("INVALID_TASK_PLAN_RECEIPT")
    if previous.get("task_plan") is not None:
        if previous["task_plan"] != plan:
            raise OperationRefused("TASK_PLAN_CONFLICT")
        raise OperationRefused("TASK_PLAN_RETRY_REQUIRES_ORIGINAL_PUBLICATION")
    return previous["revision"] + 1


def _verified_readback(stored: Any, run_id: str, plan: Any, receipt: Any) -> Any:
    """Execution requires the published content at or beyond its receipt revision."""
    if stored is None:
        raise OperationRefused("INVALID_TASK_PLAN_RECEIPT")
    content = stored.model_dump(mode="json")
    if (
        content["run_id"] != run_id
        or content["task_plan"] != plan
        or content["revision"] < receipt.revision
    ):
        raise OperationRefused("INVALID_TASK_PLAN_RECEIPT")
    return content["task_plan"]


def _unique_proposal_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate proposal key")
        value[key] = item
    return value


def _decode_proposal(output: str) -> dict[str, Any]:
    """Bound untrusted model output and reject duplicate keys before validation."""
    if len(output.encode("utf-8")) > 524288:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "task_plan"})
    return Proposal.model_validate(
        json.loads(output, object_pairs_hook=_unique_proposal_fields)
    ).model_dump()


def _planning_limits(config: Any, budget: Any) -> tuple[int, int]:
    """Reserve a model request without expanding the caller or operator budgets."""
    requests = min(
        config.request_limit,
        budget.request_limit
        if budget.request_limit is not None
        else config.request_limit,
    )
    tokens = min(
        config.total_tokens_limit,
        budget.total_tokens_limit
        if budget.total_tokens_limit is not None
        else config.total_tokens_limit,
    )
    if requests < 2 or tokens < 1:
        raise OperationRefused("BUDGET_EXHAUSTED")
    return requests, tokens


async def admitted_run_plan(request: Any, base: Any, owner: Any) -> Any:
    """Only the private host scope may enable this execution path."""
    from arbora.agent_runs import current_native_run, resolve_model

    au, eg = _contracts()
    current = current_native_run(request)
    if current is None or current is not owner:
        raise OperationRefused("UNAVAILABLE", {"capability": "task_plan"})
    owner = current
    host = owner.hosted_invocation
    config = host.task_plan_config
    if config is None:
        raise OperationRefused("UNAVAILABLE", {"capability": "task_plan"})
    caller = caller_from_session(owner.session, credential_kind="session")
    await _admit(host, caller, "agents.run")
    requests, tokens = _planning_limits(config, base.budget)
    started = time.monotonic()
    timeout = min(config.timeout_seconds, base.budget.timeout_seconds)
    async with asyncio.timeout(timeout):
        # Text output, no tools/toolsets or structured-output tools, no retries.
        planner = Agent(
            resolve_model(base.model),
            output_type=str,
            retries=0,
            instructions="Return only a JSON object with tasks and output_task_id. Each task has task_id, prompt, depends_on. Produce a finite dependency DAG, all tasks contributing to the output. Do not include authority, budgets, policy or limits.",
            model_settings={"max_tokens": min(tokens, 16384)},
        )
        result = await planner.run(
            request.task,
            usage_limits=UsageLimits(
                request_limit=1, tool_calls_limit=0, total_tokens_limit=tokens
            ),
        )
        usage = result.usage
        owner.planning_usage.update(
            {
                key: int(getattr(usage, key, 0))
                for key in (
                    "requests",
                    "tool_calls",
                    "input_tokens",
                    "output_tokens",
                    "total_tokens",
                )
            }
        )
        proposal = _decode_proposal(result.output)
        if len(proposal["tasks"]) > min(config.max_steps, base.stop.max_steps):
            raise OperationRefused("BUDGET_EXHAUSTED")
        if owner.planning_usage["total_tokens"] >= tokens:
            raise OperationRefused("BUDGET_EXHAUSTED")
        plan = await host.evaluate_task_plan(
            caller, f"task-plan:{owner.run_id}", proposal
        )
        bounded_proposal = {
            **proposal,
            "max_concurrency": config.max_concurrency,
            "max_steps": config.max_steps,
        }
        canonical = await _publish(host, caller, owner, plan, bounded_proposal, eg)
        current_native_run(request)
        await _admit(host, caller, "agents.run")
    budget = replace(
        base.budget,
        request_limit=requests,
        total_tokens_limit=tokens,
        timeout_seconds=timeout,
        max_concurrent_requests=min(
            base.budget.max_concurrent_requests, config.max_concurrency
        ),
    )
    return replace(
        base,
        steps=(),
        budget=budget,
        initial_usage=RunUsage(
            requests=owner.planning_usage.get("requests", 0),
            tool_calls=owner.planning_usage.get("tool_calls", 0),
            input_tokens=owner.planning_usage.get("input_tokens", 0),
            output_tokens=owner.planning_usage.get("output_tokens", 0),
        ),
        initial_elapsed_seconds=time.monotonic() - started,
        stop=replace(base.stop, max_steps=min(config.max_steps, base.stop.max_steps)),
        dependency_plan=au.DependencyPlan(
            tasks=tuple(
                au.RunTask(
                    task_id=t["task_id"],
                    prompt=t["prompt"],
                    depends_on=tuple(t["depends_on"]),
                )
                for t in canonical["tasks"]
            ),
            output_task_id=canonical["output_task_id"],
            max_concurrency=canonical["max_concurrency"],
            plan_id=canonical["plan_digest"],
        ),
    )
