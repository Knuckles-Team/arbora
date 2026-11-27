"""Governed agent, durable A2A task, and RLM operation declarations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.contracts.provisioning_capacity import ProvisionRequest
from arbora.api.invoke.certification_gate import MAX_RUN_SKILLS, SkillRef
from arbora.api.ops._common import Params, bound_service
from arbora.api.ops._engine import engine_send
from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)


class AgentRunParams(Params):
    message: dict[str, Any]
    context_budget_tokens: int | None = Field(default=None, ge=256, le=1_000_000)
    #: An assistant profile for the run (ARBORA-OPS-R897.6). Arbora forwards it
    #: in the message metadata and reads nothing of the profile: admission has
    #: already run, and the engine checks the profile-use grant.
    assistant_profile_ref: str | None = Field(
        default=None, min_length=1, max_length=256
    )
    #: Instances the run needs; allocated through the SDK provisioner before dispatch
    #: (ARBORA-FLEET-R024.6). The replay key of the allocation is the request's own.
    provision: ProvisionRequest | None = None
    #: The atomic skills the run uses. Each needs its current stored attestation before
    #: the run starts (ARBORA-OPS-R872); a missing, stale or mismatched one refuses.
    skills: list[SkillRef] = Field(default_factory=list, max_length=MAX_RUN_SKILLS)


class TaskRefParams(Params):
    task_id: str = Field(min_length=1, max_length=80)


class TaskListParams(Params):
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=50, ge=1, le=100)


class ChildMessageParams(Params):
    conversation_target: str = Field(min_length=1, max_length=512)
    expected_revision: int = Field(ge=0)
    content: str = Field(min_length=1)


class ContinuationParams(Params):
    conversation_target: str = Field(min_length=1, max_length=512)
    history_revision: int = Field(ge=0)
    content: str = Field(min_length=1, max_length=100_000)


class RlmParams(Params):
    request: dict[str, Any]


class AgentResult(Params):
    value: dict[str, Any]


async def admit_workflow(context: Any, message: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve a named workflow to its registry identity before any engine starts.

    An unknown workflow refuses ``WORKFLOW_IDENTITY_UNKNOWN`` with no start
    (ARBORA-OPS-R873). A known one is read from the engine ``WorkflowRegistry`` by its
    identity, and the run carries that identity (ARBORA-OPS-R874).
    """
    metadata = message.get("metadata") or {}
    workflow_id = metadata.get("workflow_id")
    if not workflow_id:
        return message
    host = bound_service(
        context, "workflow_host", reason="workflow host is not composed"
    )
    identity = host.resolve(str(workflow_id), metadata.get("workflow_version"))
    wire = identity.model_dump(mode="json")
    await engine_send(context, "WorkflowRegistry", {"op": "read", "identity": wire})
    host.engine()
    return {**message, "metadata": {**metadata, "workflow_identity": wire}}


async def _with_role_profile(context: Any, message: Mapping[str, Any]) -> Any:
    """A role run needs the tenant business profile and carries its reference.

    The profile is one tenant-shared engine record; a tenant with none refuses
    ``PROFILE_UNSET`` before any run is admitted (ARBORA-OPS-R917.3).
    """
    metadata = message.get("metadata") or {}
    if not metadata.get("role_instance_id"):
        return message
    from arbora.api.ops.operator_surface import (
        PROFILE_KEY,
        PROFILE_NAMESPACE,
        read_profile,
    )

    _profile, version = await read_profile(context)
    reference = f"{PROFILE_NAMESPACE}/{PROFILE_KEY}@{version}"
    allowlist = await _binding_tools(context, str(metadata["role_instance_id"]))
    requested = metadata.get("tool_allowlist")
    if requested is not None:
        if not isinstance(requested, list) or not all(
            isinstance(tool, str) for tool in requested
        ):
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused("INVALID_ARGUMENT", {"field": "tool_allowlist"})
        # A request may only narrow the binding, never widen it.
        allowlist = [tool for tool in requested if tool in set(allowlist)]
    scopes = sorted(getattr(context.caller, "effective_scopes", ()) or ())
    from arbora.api.model_route import http_probe, resolve_route

    route = await resolve_route(
        context,
        str(metadata["role_instance_id"]),
        metadata.get("escalate"),
        probe=context.services.get("model_probe") or http_probe,
    )
    admitted: dict[str, Any] = {
        **metadata,
        "profile_ref": reference,
        "tool_allowlist": allowlist,
        "tool_routes": {tool: _tool_route(tool) for tool in allowlist},
        "scope_ceiling": scopes,
    }
    admitted.pop("escalate", None)
    if route is not None:
        admitted["model_route"] = route
    return {**message, "metadata": admitted}


def _tool_route(tool: str) -> str:
    """Whether a tool is a deterministic engine operation or a fleet or other tool.

    The run trace counts calls by this route, so a tenant sees how much work
    the engine took from the model (ARBORA-OPS-R919.3).
    """
    return "engine" if tool.startswith("eg.") else "tool"


async def _binding_tools(context: Any, instance_id: str) -> list[str]:
    """The tool ids the role instance binding names (ARBORA-OPS-R917.3).

    The instance is the engine library entry; an absent or foreign instance is
    one ``NOT_FOUND``. The binding already cannot widen its template, so the
    run's tool ceiling is exactly this list intersected with the caller scopes.
    """
    from arbora.api.ops.agent_library import _current

    entry = await _current(context, instance_id)
    return [
        str(tool["component_id"])
        for tool in entry.get("tools", [])
        if isinstance(tool, Mapping) and tool.get("component_id")
    ]


def _bearer_session_ref(caller: Any) -> str:
    session = getattr(caller, "session", None)
    return str(getattr(getattr(session, "actor", None), "session_ref", None) or "")


def _claimed_session_ref(caller: Any) -> str:
    claims = getattr(caller, "engine_claims", None) or {}
    return str(claims.get("session_ref") or "") if hasattr(claims, "get") else ""


def client_session_id(caller: Any) -> str:
    """The opening session of a caller: its session claim, its bearer session, else the MCP session."""
    reference = _claimed_session_ref(caller) or _bearer_session_ref(caller)
    if reference:
        return reference
    from arbora.api.mcp.caller import mcp_request_ref

    ref = mcp_request_ref()
    parts = ref.split(":", 2)
    return parts[1] if len(parts) == 3 and parts[0] == "mcp" and parts[1] != "-" else ""


def _with_client_session(context: Any, message: Mapping[str, Any]) -> Mapping[str, Any]:
    """Attribute a run to the session that opened it (ARBORA-IDENTITY-R026.3)."""
    caller = context.caller
    if not getattr(caller, "authenticated", False):
        return message
    session_id = client_session_id(caller)
    if not session_id:
        from arbora.api.invoke.pipeline import OperationRefused

        raise OperationRefused("CLIENT_SESSION_MISSING")
    return {
        **message,
        "metadata": {
            **(message.get("metadata") or {}),
            "client_session_id": session_id,
            "client_principal": str(caller.principal),
        },
    }


async def _audit_run(
    context: Any,
    op: OpSpec,
    params: Mapping[str, Any],
    result: Any,
    *,
    failed: bool = False,
) -> None:
    """One audit event for the orchestrated run (ARBORA-IDENTITY-R026.4)."""
    services = context.operation_services
    if services is None:
        return
    from arbora.api.invoke.audit import run_audit_event
    from arbora.api.registry import AuditClass

    event = run_audit_event(
        result,
        context.caller,
        op=op,
        params=params,
        result_status="failed" if failed else "ok",
        surface=getattr(context, "surface", ""),
    )
    await _write_run_event(services, event, context.caller, AuditClass.EVENT)


async def _write_run_event(
    services: Any, event: dict[str, str], caller: Any, audit_class: Any
) -> None:
    """The run event is its own audit entry: reserve it, then write against that
    reservation (the pipeline's one audit path)."""
    if event.get("surface"):
        from arbora.api.invoke.audit import run_event_caller

        caller = run_event_caller(caller)
        event["audit_ref"] = await services.audit_preflight(event, audit_class, caller)
    await services.audit_write(event, audit_class, caller)


async def _provision_for_run(
    context: Any, service: Any, params: Mapping[str, Any], message: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Allocate the run's instances first; every refusal is typed and allocates nothing."""
    request = params.get("provision")
    if request is None:
        return message
    import asyncio

    if not isinstance(request, ProvisionRequest):
        request = ProvisionRequest.model_validate(request)

    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.fleet.provisioning import ProvisionRefused
    from arbora.pin_lag.unavailable import PinLagUnavailable

    if request.tenant != str(context.caller.tenant):
        raise OperationRefused("PROVISION_GRANT_DENIED", {})
    try:
        result = await asyncio.to_thread(service.authority.provision_capacity, request)
    except ProvisionRefused as refused:
        raise OperationRefused(refused.code, dict(refused.details)) from None
    except PinLagUnavailable as lag:
        raise OperationRefused("UNAVAILABLE", {"missing": lag.missing}) from None
    return {
        **message,
        "metadata": {
            **(message.get("metadata") or {}),
            "provisioned_instances": list(result.instance_ids),
            "allocation_receipt_digest": result.allocation_receipt_digest,
        },
    }


async def _attest_run(
    context: Any, params: Mapping[str, Any], message: Mapping[str, Any]
) -> None:
    """Gate the run on its graph skills: the agent's and workflow's definitions.

    A run that names no agent runs on the default fleet agent, whose definition the host
    registers in the ``AgentLibrary`` (ARBORA-HOST-R008.1001).
    """
    from arbora.agent_host.default_agent import run_agent_name
    from arbora.api.invoke.certification_gate import require_run_attestation

    agent = await run_agent_name(context, message)
    await require_run_attestation(
        context,
        params.get("skills"),
        agents=(agent,),
        workflow=(message.get("metadata") or {}).get("workflow_identity"),
    )


async def handle_agent(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Use the serving A2A authority, which owns task and idempotency state."""
    from arbora.security.session import use_session

    if context.caller.session is None:
        raise RuntimeError("agent operation requires a verified session")
    service = context.services.get("a2a")
    if service is None:
        raise RuntimeError("A2A authority is not bound")
    with use_session(context.caller.session):
        if op.id == "agents.run":
            if not context.idempotency_key:
                raise ValueError("Idempotency-Key is required")
            from arbora.modality.models import ModalityRefused

            message = await _with_role_profile(context, params["message"])
            message = await admit_workflow(context, message)
            await _attest_run(context, params, message)
            if params.get("assistant_profile_ref"):
                message = {
                    **message,
                    "metadata": {
                        **(message.get("metadata") or {}),
                        "assistant_profile_ref": params["assistant_profile_ref"],
                    },
                }
            message = _with_client_session(context, message)
            message = await _provision_for_run(context, service, params, message)
            from arbora.a2a.source_grant import (
                resolve_verified_grant,
                source_grant_scope,
            )

            grant = await resolve_verified_grant(context)
            try:
                with source_grant_scope(grant):
                    result = await service.send_message(
                        message=message,
                        idempotency_key=context.idempotency_key,
                        context_budget_tokens=params.get("context_budget_tokens"),
                    )
            except ModalityRefused as refusal:
                # ARBORA-OPS-R899.3: a typed refusal on MCP, HTTP and A2A alike.
                from arbora.api.invoke.pipeline import OperationRefused

                await _audit_run(context, op, params, {}, failed=True)
                raise OperationRefused(refusal.arbora_code, refusal.details()) from None
            except Exception:
                await _audit_run(context, op, params, {}, failed=True)
                raise
            await _audit_run(context, op, params, result)
        elif op.id == "agents.tasks.get":
            result = await service.get_task(params["task_id"])
            if result is None:
                raise LookupError("A2A task not found")
        elif op.id == "agents.tasks.list":
            result = await service.list_tasks(
                cursor=params.get("cursor"), limit=params["limit"]
            )
        elif op.id in ("agents.tasks.message", "agents.tasks.continue"):
            if not context.idempotency_key:
                raise ValueError("Idempotency-Key is required")
            authority = service.authority
            from arbora.a2a.authority import A2AIdempotencyConflict
            from arbora.api.invoke.pipeline import OperationRefused

            try:
                if op.id == "agents.tasks.message":
                    receipt = await authority.admit_child_message(
                        conversation_target=params["conversation_target"],
                        expected_revision=params["expected_revision"],
                        content=params["content"],
                        idempotency_key=context.idempotency_key,
                    )
                else:
                    receipt = await authority.admit_continuation(
                        conversation_target=params["conversation_target"],
                        history_revision=params["history_revision"],
                        content=params["content"],
                        idempotency_key=context.idempotency_key,
                    )
            except A2AIdempotencyConflict:
                raise OperationRefused("IDEMPOTENCY_CONFLICT", {}) from None
            return {"value": receipt}
        elif op.id == "agents.tasks.cancel":
            result = await service.cancel_task(params["task_id"])
        else:
            raise ValueError("unknown agent operation")
    return {"value": result.model_dump(mode="json", by_alias=True)}


async def handle_rlm(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Compose AU's public RLM control plane with the verified caller context."""
    from pydantic import TypeAdapter

    from arbora.api.contracts import GraphRlmRequest, compose_agent_control_plane

    if context.caller.session is None:
        raise RuntimeError("RLM operation requires a verified session")
    request: Any = TypeAdapter(GraphRlmRequest).validate_python(params["request"])
    action = op.id.rsplit(".", 1)[-1]
    if request.action != action:
        raise ValueError("RLM action does not match operation")
    control_plane = compose_agent_control_plane(
        context.client,
        context.caller.session,
        origins=bound_service(
            context, "run_origins", reason="run origin registry is not composed"
        ),
    )
    result = await control_plane.graph_rlm(request)
    return {"value": result.model_dump(mode="json")}


#: Operations whose idempotency the A2A WorkItem authority owns: the
#: idempotency key derives the task id, so a replay returns the same task.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "agents.run": "arbora.a2a.authority:WorkItemA2AAuthority",
    "agents.tasks.message": "arbora.a2a.authority:WorkItemA2AAuthority",
    "agents.tasks.continue": "arbora.a2a.authority:WorkItemA2AAuthority",
}


def operations() -> tuple[OpSpec, ...]:
    """Operation records consumed by the central registry and all projections."""
    handler = Composite(handler="arbora.api.ops.agents.handle_agent")
    rlm_handler = Composite(handler="arbora.api.ops.agents.handle_rlm")
    return (
        OpSpec(
            id="agents.run",
            verb=Verb.ACT,
            summary="Run a governed agent task",
            examples=("run an agent to investigate this issue",),
            params=AgentRunParams,
            result=AgentResult,
            binding=handler,
            scopes=frozenset({"kg:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="agents.tasks.get",
            verb=Verb.ASK,
            summary="Get an owned agent task",
            examples=("show the status of this agent task",),
            params=TaskRefParams,
            result=AgentResult,
            binding=handler,
            scopes=frozenset({"kg:read"}),
        ),
        OpSpec(
            id="agents.tasks.list",
            verb=Verb.ASK,
            summary="List owned agent tasks",
            examples=("list my agent tasks",),
            params=TaskListParams,
            result=AgentResult,
            binding=handler,
            scopes=frozenset({"kg:read"}),
        ),
        OpSpec(
            id="agents.tasks.cancel",
            verb=Verb.ACT,
            summary="Cancel an owned agent task",
            examples=("cancel this agent task",),
            params=TaskRefParams,
            result=AgentResult,
            binding=handler,
            scopes=frozenset({"kg:write"}),
            effect=Effect.WRITE,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="agents.tasks.message",
            verb=Verb.ACT,
            summary="Send one message to an active child run",
            examples=("send this message to the running child agent",),
            params=ChildMessageParams,
            result=AgentResult,
            binding=handler,
            scopes=frozenset({"kg:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="agents.tasks.continue",
            verb=Verb.ACT,
            summary="Continue a completed child run with a new turn",
            examples=("continue the completed child agent conversation",),
            params=ContinuationParams,
            result=AgentResult,
            binding=handler,
            scopes=frozenset({"kg:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
        *(
            OpSpec(
                id=f"rlm.{action}",
                verb=Verb.ACT,
                summary=f"Execute governed RLM {action.replace('_', ' ')}",
                examples=(f"{action.replace('_', ' ')} with RLM",),
                params=RlmParams,
                result=AgentResult,
                binding=rlm_handler,
                scopes=frozenset({"kg:write"}),
                effect=Effect.WRITE,
                audit=AuditClass.EVENT,
            )
            for action in ("run",)
        ),
    )


specs = operations
