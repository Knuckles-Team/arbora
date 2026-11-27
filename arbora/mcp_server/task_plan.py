"""Evaluate untrusted task graphs through the shared host authority.

The returned content is not a durable plan or an execution grant. Publication
under EG's native WorkItem owner and lease fence must precede runtime
consumption. Public scaffold projection never carries executable prompts.
"""

from __future__ import annotations

from collections.abc import Mapping
from importlib import import_module
from typing import Any

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.executor import execution_context
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.pipeline_authority import authorize
from arbora.api.invoke.pipeline_services import InvokeServices
from arbora.api.invoke.steps import RequestCaller
from arbora.core.claims import claim_values, freeze_claims
from arbora.core.settings import RuntimeConfig
from arbora.core.task_plan_config import TaskPlanConfig
from arbora.security.operation_result import OpError


def _contract() -> tuple[Any, Any, Any, Any]:
    try:
        client = import_module("medulla.decision_client")
        models = import_module("medulla.generated.models")
        query = import_module("medulla.generated.query")
        members = (
            getattr(client, "task_plan_request", None),
            getattr(client, "verify_task_plan_receipt", None),
            getattr(query, "send_decide", None),
            getattr(query, "decode_decide", None),
        )
        if not all(callable(member) for member in members) or not hasattr(
            models, "TaskPlanProposal"
        ):
            raise ImportError("task plan contract absent")
    except ImportError as exc:
        raise OperationRefused(
            "UPGRADE_REQUIRED", {"capability": "medulla.task_plan"}
        ) from exc
    return members


async def _admit(services: InvokeServices, caller: RequestCaller) -> Any:
    selected = None
    for op_id in ("agents.run", "eg.query.Decide"):
        op = services.registry.get(op_id)
        if op is None:
            raise OperationRefused("UNAVAILABLE", {"operation": op_id})
        refused = await authorize(op, {}, caller, services, None)
        if isinstance(refused, OpError):
            raise OperationRefused(refused.code, refused.details)
        selected = op
    return selected


async def _published_feature_schema(
    services: InvokeServices, caller: RequestCaller
) -> Mapping[str, Any]:
    """Read the caller tenant's published schema pin; never publish it.

    ``TaskPlanConfig.feature_schema`` is operator configuration, not a durable
    AgentComponent revision.  The engine mints the definition digest when the
    server lifespan publishes the body for a tenant, so nested planning reads
    the same published pin as ``agents.plan_run``.
    """
    from arbora.api.task_plan_schema import published_pin

    bindings = getattr(services.runtime, "bindings", {})
    return published_pin(bindings.get("task_plan_schema_pins"), str(caller.tenant))


async def evaluate_task_plan(
    services: InvokeServices,
    config: TaskPlanConfig,
    caller: RequestCaller,
    question_id: str,
    proposal: Mapping[str, Any],
) -> Mapping[str, Any]:
    """Admit evaluation, not execution; do not accept caller-supplied receipts."""
    if not isinstance(proposal, Mapping) or set(proposal) != {
        "tasks",
        "output_task_id",
    }:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "task_plan"})
    request_builder, verify, send, decode = _contract()
    op = await _admit(services, caller)
    feature_schema = await _published_feature_schema(services, caller)
    try:
        request = request_builder(
            caller.tenant,
            question_id,
            {
                **claim_values(proposal),
                "max_concurrency": config.max_concurrency,
                "max_steps": config.max_steps,
            },
            claim_values(feature_schema),
            policy=claim_values(config.policy),
        )
    except (TypeError, ValueError) as exc:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "task_plan"}) from exc
    async with execution_context(op, caller, services.runtime) as context:
        graph = await caller_dispatch_graph(context)
        await _admit(services, caller)
        response = await send(context.client, {"request": request}, graph)
        await _admit(services, caller)
        try:
            records = decode(response).model_dump(mode="json", by_alias=True)["records"]
            if len(records) != 1:
                raise ValueError("response record count mismatch")
            plan = verify(records[0], request, expected_principal=caller.principal)
        except (KeyError, TypeError, ValueError) as exc:
            raise OperationRefused("INVALID_TASK_PLAN_RECEIPT") from exc
    return freeze_claims(plan)


def configured_task_plan(settings: RuntimeConfig) -> TaskPlanConfig | None:
    """Read explicit operator settings, rejecting invalid pins before startup."""
    import json

    from pydantic import TypeAdapter

    raw = settings.task_plan
    if raw is None:
        return None
    # The schema body is published and resolved per tenant at request time.
    # Keep an empty value as a migration path for older callers that still
    # construct the trusted config positionally; never require an operator to
    # invent a tenant-bound definition digest in this process setting.
    raw = dict(raw)
    raw.setdefault("feature_schema", {})
    config = TaskPlanConfig(**raw)
    _contract()
    from medulla.generated.models import ComponentDependency, DecisionPolicyRef

    if config.feature_schema:
        installed = import_module("medulla.decision_client").TASK_PLAN_FEATURE_SCHEMA
        if config.feature_schema.get("definition_digest") == installed.get(
            "content_digest"
        ):
            raise ValueError(
                "task plan feature_schema.definition_digest is an export content "
                "digest; omit the static schema pin"
            )
        ComponentDependency.model_validate_json(
            json.dumps(claim_values(config.feature_schema)), strict=True
        )
    TypeAdapter(DecisionPolicyRef).validate_json(
        json.dumps(claim_values(config.policy)), strict=True
    )
    if config.feature_schema and config.feature_schema["kind"] != "feature_schema":
        raise ValueError("task plan schema pin must name a feature_schema")
    if (
        config.policy["policy"] == "pinned"
        and config.policy["component"]["kind"] != "decision_policy"
    ):
        raise ValueError("task plan policy pin must name a decision_policy")
    return config
