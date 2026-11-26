"""The production composition of the A2A approval chain (ARBORA-A2A-R005.2.5, R005.5).

The chain is built from nothing but the engine: human grants are control leases of
``LEASE_KIND``, approval keys come from the identity authority's registered keys, and
every decision audit entry is a reserved ``audit_append`` of the verified caller. Arbora
keeps no state. A run-keyed approval has no effect of its own to run: the waiting run
reads its consumed grant, so the effect runner only acknowledges the grant.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .approval import PendingCallBinding
from .confirm import ApprovalChain, RegisteredKeyProducer
from .engine_deciders import AskDecide, EngineHeldItemDecider, EngineToolActionDecider
from .grant_store import HumanGrantStore
from .held_item_admission import HeldItemAdmitter, HeldItemDecider
from .tool_action_admission import ToolActionAdmitter, ToolActionDecider

LEASE_KIND = "arbora.human_grant"


class LazyKeys:
    """``HumanApprovalKeyAuthority`` over a client resolved when a key is read."""

    def __init__(
        self, client: Callable[[], Awaitable[Any]], *, graph: str, tenant: str
    ) -> None:
        self._client = client
        self._graph = graph
        self._tenant = tenant

    async def resolve_key(self, key_id: str, *, principal_id: str, purpose: str) -> Any:
        from arbora.identity.approval_engine import EngineHumanApprovalKeys

        keys = EngineHumanApprovalKeys(
            await self._client(), graph=self._graph, tenant=self._tenant
        )
        return await keys.resolve_key(
            key_id, principal_id=principal_id, purpose=purpose
        )


def audit_sink(
    adapter: Any, current_session: Callable[[], Any]
) -> Callable[[Mapping[str, Any]], Awaitable[None]]:
    """One reserved audit entry per decision: the digest of its references, no values."""

    async def record(entry: Mapping[str, Any]) -> None:
        from arbora.api.mcp.caller import caller_from_session
        from arbora.api.registry import AuditClass

        session = current_session()
        if session is None:
            raise PermissionError("a verified session is required to audit")
        digest = hashlib.sha256(
            json.dumps(dict(entry), sort_keys=True, default=repr).encode()
        ).hexdigest()
        caller = caller_from_session(
            session,
            credential_kind="session",
            request_id=f"a2a-confirm:{uuid.uuid4().hex}",
        )
        event = {
            "op": "a2a.plan.confirm",
            "surface": "a2a",
            "principal": caller.principal,
            "tenant": caller.tenant,
            "plan_digest": digest,
            "result_status": "ok",
            "request_id": caller.request_id,
        }
        await adapter.preflight(event, AuditClass.EVENT, caller)

    return record


async def acknowledge(binding: PendingCallBinding) -> Mapping[str, Any]:
    """The approved call is run by its waiting run; the grant is the acknowledgement."""
    return {"acknowledged": binding.plan.op_id, "grant_id": binding.grant_id}


def compose_approval_chain(
    *,
    leases: Any,
    client: Callable[[], Awaitable[Any]],
    graph: str,
    tenant: str,
    audit_adapter: Any,
    current_session: Callable[[], Any],
    tool_decider: ToolActionDecider | None = None,
    held_decider: HeldItemDecider | None = None,
    ask_decide: AskDecide | None = None,
    timeout_s: float | None = None,
) -> ApprovalChain:
    """Grant store, key authority, audit and the two admitters over the engine.

    ``ask_decide`` binds both engine deciders over one ``Decide`` seam; an explicit
    decider wins. With neither, the admitter stays unbound and confirm refuses closed.
    """
    if ask_decide is not None:
        tool_decider = tool_decider or EngineToolActionDecider(ask_decide)
        held_decider = held_decider or EngineHeldItemDecider(ask_decide)
    audit = audit_sink(audit_adapter, current_session)
    return ApprovalChain(
        HumanGrantStore(leases, lease_kind=LEASE_KIND),
        RegisteredKeyProducer(LazyKeys(client, graph=graph, tenant=tenant)),
        acknowledge,
        audit,
        tool_actions=(
            ToolActionAdmitter(tool_decider, audit, timeout_s=timeout_s)
            if tool_decider is not None
            else None
        ),
        held_items=(
            HeldItemAdmitter(held_decider, audit, timeout_s=timeout_s)
            if held_decider is not None
            else None
        ),
    )


def run_plan_binding(services: Any) -> Callable[[Any, str, str], Any]:
    """``plan_for(owner, kind, digest)``: the binding a run-keyed confirmation signs."""
    from arbora.api.invoke.plan import PlanBinding

    def plan_for(owner: Any, kind: str, digest: str) -> Any:
        session = owner.session
        return PlanBinding(
            op_id=f"run.confirm.{kind}",
            params_digest=digest,
            principal=session.actor.actor_id,
            tenant=session.tenant,
            policy_revision=str(session.policy_version),
            registry_digest=services.registry.digest,
            effect="write",
            confirm="plan",
        )

    return plan_for
