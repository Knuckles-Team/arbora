"""Evaluate untrusted task graphs through the shared host authority.

The returned content is not a durable plan or an execution grant. Publication
under EG's native WorkItem owner and lease fence must precede runtime
consumption. Public scaffold projection never carries executable prompts.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any, Literal

from arbora.api.invoke.bootstrap import _caller_dispatch_graph
from arbora.api.invoke.executor import execution_context
from arbora.api.invoke.pipeline import InvokeServices, OperationRefused, _authorize
from arbora.api.invoke.steps import OpError, RequestCaller, claim_values, freeze_claims


@dataclass(frozen=True)
class TaskPlanConfig:
    """Trusted composition settings, never fields of a model proposal."""

    # The schema is published per tenant at request time.  Keep the field for
    # positional/config compatibility, but an operator may omit it entirely.
    feature_schema: Mapping[str, Any] = field(default_factory=dict)
    policy: Mapping[str, Any] = field(default_factory=dict)
    max_concurrency: int = 0
    max_steps: int = 0
    request_limit: int = 50
    total_tokens_limit: int = 100_000
    timeout_seconds: float = 600.0
    plan_scaffold: Literal["on", "off"] = "off"

    def __post_init__(self) -> None:
        if type(self.plan_scaffold) is not str or self.plan_scaffold not in (
            "on",
            "off",
        ):
            raise ValueError("plan_scaffold must be on or off")
        if not isinstance(self.feature_schema, Mapping) or not isinstance(
            self.policy, Mapping
        ):
            raise ValueError("task plan schema and policy must be mappings")
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 64:
            raise ValueError("max_steps must be between 1 and 64")
        _validate_budgets(
            self.request_limit, self.total_tokens_limit, self.timeout_seconds
        )
        object.__setattr__(self, "feature_schema", freeze_claims(self.feature_schema))
        object.__setattr__(self, "policy", freeze_claims(self.policy))


def _validate_budgets(
    request_limit: int, total_tokens_limit: int, timeout_seconds: float
) -> None:
    """Reject non-integral budgets and non-finite deadlines before composition."""
    import math

    for value in (request_limit, total_tokens_limit):
        if type(value) is not int or value < 1:
            raise ValueError("task plan budgets must be positive integers")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("task plan timeout must be finite and positive")


def _contract() -> tuple[Any, Any, Any, Any]:
    try:
        client = import_module("epistemic_graph.decision_client")
        models = import_module("epistemic_graph.generated.models")
        query = import_module("epistemic_graph.generated.query")
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
            "UPGRADE_REQUIRED", {"capability": "epistemic-graph.task_plan"}
        ) from exc
    return members


async def _admit(services: InvokeServices, caller: RequestCaller) -> Any:
    selected = None
    for op_id in ("agents.run", "eg.query.Decide"):
        op = services.registry.get(op_id)
        if op is None:
            raise OperationRefused("UNAVAILABLE", {"operation": op_id})
        refused = await _authorize(op, {}, caller, services, None)
        if isinstance(refused, OpError):
            raise OperationRefused(refused.code, refused.details)
        selected = op
    return selected


async def _published_feature_schema(
    services: InvokeServices, caller: RequestCaller
) -> Mapping[str, Any]:
    """Resolve the caller's tenant-bound schema through the served publisher.

    ``TaskPlanConfig.feature_schema`` is operator configuration, not a durable
    AgentComponent revision.  The engine mints the definition digest when the
    schema body is published for a tenant, so nested planning must use the same
    verified publisher binding as ``agents.plan_run``.
    """
    bindings = getattr(services.runtime, "bindings", {})
    pins = bindings.get("task_plan_schema_pins")
    publish = bindings.get("task_plan_schema_publish")
    if pins is None or publish is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "no verified task-plan schema pin"}
        )
    try:
        return await pins.for_tenant(str(caller.tenant), publish)
    except OperationRefused:
        raise
    except Exception as exc:
        raise OperationRefused(
            "UNAVAILABLE",
            {"reason": f"task-plan schema publish failed: {type(exc).__name__}"},
        ) from exc


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
        graph = await _caller_dispatch_graph(context)
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


def configured_task_plan() -> TaskPlanConfig | None:
    """Read explicit operator settings, rejecting invalid pins before startup."""
    import json

    from pydantic import TypeAdapter

    from arbora.core.settings import runtime_config

    raw = runtime_config.task_plan
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
    from epistemic_graph.generated.models import ComponentDependency, DecisionPolicyRef

    if config.feature_schema:
        installed = import_module(
            "epistemic_graph.decision_client"
        ).TASK_PLAN_FEATURE_SCHEMA
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
