"""Task-plan schema publication and the deployment library startup the served invocation composes (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from arbora.access.action_policy import ActionPolicies, ActionPolicy
from arbora.api.registry import EgSchemaRef


def validate_provider_schema(
    model: EgSchemaRef, params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Admit ``params`` as the input of the engine method ``model`` names.

    The engine's run-time method catalog carries no request schemas; the
    engine validates the request shape itself. Arbora admits a JSON object:
    a mapping with string keys whose values serialize as JSON. Anything else
    raises :class:`SchemaMismatch` naming the fields, never their values; the
    invoke pipeline maps it to ``INVALID_ARGUMENT``.
    """
    if not isinstance(model, EgSchemaRef):
        raise ValueError("invalid EG schema reference")
    if not isinstance(params, Mapping):
        raise SchemaMismatch(
            {"reason": "input is not an object", "invalid": ["$ (type)"]}
        )
    if any(not isinstance(key, str) for key in params):
        raise SchemaMismatch(
            {"reason": "input field names must be strings", "invalid": ["$ (keys)"]}
        )
    invalid = sorted(key for key, value in params.items() if not _is_json(value))
    if invalid:
        raise SchemaMismatch(
            {
                "reason": "input does not serialize as JSON",
                "invalid": [f"{key} (json)" for key in invalid][:16],
            }
        )
    return dict(params)


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


class SchemaMismatch(ValueError):
    """An input that does not match its EG schema, named by field only.

    ARBORA-OPS-R009.4: ``details`` names the invalid fields and the check each
    failed. It never carries an input value.
    """

    def __init__(self, details: Mapping[str, Any]) -> None:
        super().__init__("operation input does not match its EG schema")
        self.details = dict(details)


def _schema_action(session: Any, graph: str, pin: Mapping[str, Any]) -> Any:
    """The exact schema intent considered by the existing action-policy owner."""
    from arbora.api.contracts import ActionRequest
    from arbora.api.task_plan_schema import COMPONENT_VERSION, PURPOSE_ID

    return ActionRequest(
        kind=PURPOSE_ID,
        target=pin["component_id"],
        params={
            "component_kind": pin["kind"],
            "component_version": COMPONENT_VERSION,
            "schema_version": pin["schema_version"],
            "content_digest": pin["content_digest"],
            "engine_version": pin["engine_version"],
            "tenant_id": session.tenant,
            "graph": graph,
            "policy_revision": str(session.policy_version),
        },
        source="arbora",
        reason="authorize the pinned task-plan feature schema publication",
        actor_id=str(session.actor.actor_id),
    )


def _publication_session(
    session: Callable[[], Any], tenant: str, graph: str, scopes: Sequence[str]
) -> Any:
    """The verified process session, holding one of ``scopes`` for ``tenant``.

    The engine classifies each publish as an exact admin or write action. A
    service identity carrying that exact capability may use it directly; the
    engine also treats ``kg:admin`` as the aggregate grant for every admin
    action. Keep the aggregate fallback here rather than lowering publication to
    the ordinary ``kg:write`` capability, which the engine deliberately refuses.
    """
    from arbora.security.session import ScopeError, resolve_session

    verified = resolve_session(session(), graph=graph)
    for scope in scopes:
        try:
            verified.require_scope(scope)
            break
        except ScopeError:
            continue
    else:
        verified.require_scope("kg:admin")
    if verified.tenant != tenant or not str(verified.policy_version).strip():
        raise PermissionError(
            "library publication needs its verified tenant and revision"
        )
    return verified


def _schema_session(session: Callable[[], Any], tenant: str, graph: str) -> Any:
    return _publication_session(session, tenant, graph, ("admin:decision-catalog",))


async def _receipted_publish_context(
    policy: ActionPolicy,
    verify: Callable[[], Any],
    action_for: Callable[[Any], Any],
    purpose_id: str,
) -> dict[str, Any]:
    """Require a new durable receipt for ``action_for(session)``, then bind it."""
    import asyncio

    from arbora.api.contracts import PolicyDisposition, PolicyReceipt
    from arbora.api.contracts.provisioning import opaque_principal
    from arbora.api.domain.mcp_server.decide_wiring import mutation_context

    request = action_for(verify())
    digest = request.digest()
    decision = await asyncio.to_thread(policy.decide, request)
    verified = verify()
    if action_for(verified).digest() != digest:
        raise PermissionError("publication intent changed during policy decision")
    receipt = decision.receipt
    if not (
        decision.allowed
        and decision.disposition is PolicyDisposition.APPROVE
        and isinstance(receipt, PolicyReceipt)
        and receipt.authorizes_effect
        and receipt.receipt_id == decision.audit_id
        and receipt.request_digest == digest
        and decision.request.digest() == digest
    ):
        raise PermissionError("publication has no exact effect-authorizing receipt")
    context = mutation_context(verified, purpose_id)
    # AgentComponent validates the caller-shaped mutation context before EG's
    # admitted-context binder replaces its policy and process-owner fields.
    # Use the same canonical opaque principal derivation as connector-pack
    # provisioning; the native binder still authenticates and replaces the
    # authoritative carrier, tenant, and policy values.
    opaque_caller = opaque_principal(str(verified.actor.actor_id))
    context.update(
        principal=opaque_caller,
        caller_principal=opaque_caller,
        actor_scope=opaque_caller,
    )
    return {
        **context,
        "policy_digest": f"sha256:{receipt.request_digest}",
        "policy_decision_id": receipt.receipt_id,
    }


async def _schema_publish_context(
    policy: ActionPolicy,
    session: Callable[[], Any],
    graph: str,
    tenant: str,
    pin: Mapping[str, Any],
) -> dict[str, Any]:
    """Require a new durable receipt; default policy still holds this action."""
    from arbora.api.task_plan_schema import PURPOSE_ID

    return await _receipted_publish_context(
        policy,
        lambda: _schema_session(session, tenant, graph),
        lambda verified: _schema_action(verified, graph, pin),
        PURPOSE_ID,
    )


def _task_plan_schema_publisher(
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
    action_policies: ActionPolicies,
) -> Callable[[str, Mapping[str, Any]], Coroutine[Any, Any, str]]:
    """The process-lifecycle publisher; it installs the process authority."""
    from arbora.api.task_plan_schema import engine_publish
    from arbora.mcp_server.served_engine import ServedEngine
    from arbora.security.session import resolve_session, use_session

    async def client_for(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    engine = ServedEngine(pool, session)

    async def context_for(tenant: str, pin: Mapping[str, Any]) -> dict[str, Any]:
        policy = action_policies.for_engine(engine)
        return await _schema_publish_context(policy, session, graph, tenant, pin)

    @asynccontextmanager
    async def bind_client(tenant: str) -> AsyncIterator[Any]:
        # AgentComponent's generated senders operate on the raw EG transport.
        # Bind the process authority for the exact tenant/graph around both the
        # current read and the publish so client_scope is restored per call
        # and concurrent tenants cannot inherit one another's claims.
        async with pool.bind(connect_context(tenant), graph) as client:
            yield client

    publisher = engine_publish(client_for, context_for, graph, bind_client=bind_client)

    async def publish(tenant: str, pin: Mapping[str, Any]) -> str:
        with use_session(session()):
            verified = resolve_session(graph=graph)
            if verified.tenant != tenant:
                raise PermissionError("schema publication cannot retarget its tenant")
            return await publisher(tenant, pin)

    return publish


def _library_action(session: Any, graph: str, tenant: str) -> Any:
    """The exact deployment-library intent considered by the action-policy owner."""
    from arbora.api.contracts import ActionRequest
    from arbora.deployment.assembly_library import PURPOSE_ID, ROLES, TARGETS

    return ActionRequest(
        kind=PURPOSE_ID,
        target=f"deployment:{tenant}",
        params={
            "targets": list(TARGETS),
            "slots": list(ROLES),
            "tenant_id": session.tenant,
            "graph": graph,
            "policy_revision": str(session.policy_version),
        },
        source="arbora",
        reason="authorize the deployment templates and tool components",
        actor_id=str(session.actor.actor_id),
    )


def _deployment_library_startup(
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
    action_policies: ActionPolicies,
) -> Callable[[], Coroutine[Any, Any, Any]]:
    """The process-lifecycle publisher of the deployment library (R017.20).

    Every write binds the process authority to the exact tenant and graph, under
    a fresh receipted mutation context. A request binding never reaches it.
    """
    from arbora.deployment import assembly_library as library
    from arbora.mcp_server.served_engine import ServedEngine
    from arbora.security.session import resolve_session, use_session

    engine = ServedEngine(pool, session)
    tenant = str(session().tenant)

    async def context_for(for_tenant: str, key: str) -> Mapping[str, Any]:
        policy = action_policies.for_engine(engine)
        scopes = ("agent:component-write", "agent:template-write", "agent:graph-write")
        context = await _receipted_publish_context(
            policy,
            lambda: _publication_session(session, for_tenant, graph, scopes),
            lambda verified: _library_action(verified, graph, for_tenant),
            library.PURPOSE_ID,
        )
        return {**context, "idempotency_key": key}

    async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
        from medulla.generated import storage

        sender = {
            "AgentComponent": storage.send_agent_component,
            "AgentTemplate": storage.send_agent_template,
            "AgentGraph": storage.send_agent_graph,
        }[method]
        async with pool.bind(connect_context(tenant), graph) as client:
            with use_session(session()):
                resolve_session(graph=graph)
                return await sender(client, params, graph, idempotency_key=key)

    async def startup() -> dict[str, dict[str, bool]]:
        bindings = library.load_tool_bindings()
        return await library.publish_at_startup(send, context_for, (tenant,), bindings)

    return startup


def _rank_schema_lookup(bindings: Mapping[str, Any] | None) -> Any:
    """``(tenant, question_id)`` to the published ranking-schema pin, or ``None``."""
    from arbora.api.task_plan_schema import rank_pin

    return lambda tenant, question_id: rank_pin(
        (bindings or {}).get("catalog_rank_schema_pins"), tenant, question_id
    )


def _with_task_plan_schema(
    bindings: Mapping[str, Any] | None,
    pool: Any,
    graph: str,
    connect_context: Callable[[str], Any],
    session: Callable[[], Any],
    action_policies: ActionPolicies,
) -> tuple[Mapping[str, Any] | None, Callable[[], Coroutine[Any, Any, Any]] | None]:
    """Bind the verified task-plan schema pin (ARBORA-RELEASE-R003.4.4).

    The committed body binds only when its digest matches the installed
    engine's export. The request bindings get the read-only pins. The returned
    start-up callable is the process-lifecycle publisher: the server lifespan
    awaits it under the process authority for every served tenant. No request
    binding can reach it, so a request never borrows the process authority.
    Until a tenant is published, ``agents.plan_run`` refuses ``UNAVAILABLE``.
    """
    if bindings is None or "task_plan_schema_pins" in bindings:
        return bindings, None
    from arbora.api.task_plan_schema import (
        catalog_rank_pins,
        process_schema_pins,
        publish_all,
        startup_pins,
    )

    pins = startup_pins()
    # The process control-step schemas publish and pin the same way as the ranking ones
    # (ARBORA-OPS-R898.5): both are keyed by question id.
    rank_pins = {**catalog_rank_pins(), **process_schema_pins()}
    if not any((pins, rank_pins)):
        return bindings, None
    publish = _task_plan_schema_publisher(
        pool, graph, connect_context, session, action_policies
    )

    async def publish_served_tenants() -> dict[str, bool]:
        # Arbora serves the process tenant's graph only: the engine pool binds
        # each graph to one tenant. The ranking schemas publish the same way, once
        # per tenant (MEDULLA-DECISION-ENGINE-R829.4: the engine does not auto-publish).
        tenants = (str(session().tenant),)
        return await publish_all(pins, rank_pins, publish, tenants)

    return _schema_bindings(bindings, pins, rank_pins), publish_served_tenants


def _schema_bindings(
    bindings: Mapping[str, Any], pins: Any, rank_pins: Mapping[str, Any]
) -> dict[str, Any]:
    """The request bindings that read the published schema pins (read-only)."""
    bound = {**bindings, "catalog_rank_schema_pins": rank_pins}
    if pins is not None:
        bound["task_plan_schema_pins"] = pins
    return bound
