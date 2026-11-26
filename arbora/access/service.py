"""Access services for the Arbora operation registry.

The operation pipeline supplies the verified caller and tenant-bound EG client.
No handler accepts identity or tenant from operation parameters.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.access.approval_leases import (
    ACTION_APPROVAL_KIND,
    APPROVAL_ID_PREFIX,
    approval_lease_to_props,
)
from arbora.access.external_policy_sync import (
    Applier,
    ApplyReport,
    BundleFetcher,
    clear_dirty_state,
)
from arbora.security.elevation import (
    ElevationApproval,
    ElevationReceipt,
    ElevationRefused,
    ElevationRequest,
    ElevationRevocation,
    ElevationService,
    ElevationSurface,
)


def sync_external_policy(
    fetcher: BundleFetcher,
    applier: Applier,
    *,
    tenant: str,
    graphs: list[str],
    marking_names: list[str],
) -> ApplyReport:
    """Fetch the engine's row-visibility bundle and apply it to every target.

    A failed fetch fails closed inside ``Applier.apply_all``. The tenant's dirty
    flag clears after the cycle, so the next marking change dirties it again.
    """
    fetched = fetcher.fetch(tenant=tenant, graphs=graphs, marking_names=marking_names)
    report = applier.apply_all(fetched)
    clear_dirty_state(tenant)
    return report


def _elevations(context: Any) -> ElevationService:
    return ElevationService(context.client, caller=context.caller.principal)


def _console_claims(context: Any) -> Mapping[str, Any]:
    """Elevations use their existing exact-scope and direct-caller checks."""
    claims = dict(context.caller.engine_claims)
    claims["agent_id"] = context.caller.principal
    claims["scopes"] = sorted(context.caller.effective_scopes)
    if context.caller.delegated:
        claims["delegation"] = [context.caller.principal, "delegated"]
    return claims


def _written(context: Any, view: Any) -> dict[str, Any]:
    """The view of one write with its receipt (ARBORA-IDENTITY-R001.1)."""
    receipt = ElevationReceipt(
        audit_ref=getattr(context, "audit_ref", "") or "",
        request_digest=view.request_digest,
    )
    return {**view.model_dump(mode="json"), "receipt": receipt.model_dump()}


def _refusal(exc: ElevationRefused) -> Exception:
    """A surface-side or engine elevation refusal under its own stable code."""
    from arbora.api.errors import ArboraErrorCode, EngineRefusal
    from arbora.api.invoke.pipeline import OperationRefused

    try:
        ArboraErrorCode(exc.code)
    except ValueError:
        return EngineRefusal(exc.code)
    return OperationRefused(exc.code)


async def request_elevation(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    ask = ElevationRequest.model_validate(params)
    try:
        return _written(context, await _elevations(context).request(ask))
    except ElevationRefused as exc:
        raise _refusal(exc) from exc


async def list_elevations(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    views = await _elevations(context).list_elevations()
    return {"elevations": [view.model_dump(mode="json") for view in views]}


async def revoke_elevation(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    revocation = ElevationRevocation.model_validate(params)
    try:
        return _written(context, await _elevations(context).revoke(revocation))
    except ElevationRefused as exc:
        raise _refusal(exc) from exc


async def approve_elevation(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    approval = ElevationApproval.model_validate(params)
    try:
        view = await _elevations(context).approve(
            approval,
            surface=ElevationSurface.OPERATOR_CONSOLE,
            claims=_console_claims(context),
        )
    except ElevationRefused as exc:
        raise _refusal(exc) from exc
    return _written(context, view)


async def deny_elevation(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Keep the unsupported design operation distinct from EG revoke."""
    raise NotImplementedError(  # ABSTRACT-OK: no native EG deny verb exists yet
        "ELEVATION_DENY_UNAVAILABLE: native EG deny is required"
    )


async def list_leases(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    return await context.client.control_leases.list(
        tenant=context.caller.tenant,
        kind=params.get("kind"),
        status=params.get("status"),
        cursor=params.get("cursor"),
        limit=params.get("limit", 100),
    )


async def get_lease(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    lease = await context.client.control_leases.get(
        tenant=context.caller.tenant, lease_id=params["lease_id"]
    )
    if lease is None:
        raise LookupError("control lease is unavailable")
    return lease


async def check_access(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    allowed = await context.client.consensus.check_access(
        params["agent_id"], params["action"], graph=params["graph"]
    )
    return {"allowed": bool(allowed)}


async def explain_policy(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    return await context.client.query.explain_policy(params["plan"])


async def list_approvals(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    page = await context.client.control_leases.list(
        tenant=context.caller.tenant,
        kind=ACTION_APPROVAL_KIND,
        status=params.get("status", "active"),
        cursor=params.get("cursor"),
        limit=params.get("limit", 100),
    )
    return {
        "approvals": [
            approval_lease_to_props(lease) for lease in page.get("leases", [])
        ],
        "next_cursor": page.get("next_cursor"),
    }


async def get_approval(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    lease = await context.client.control_leases.get(
        tenant=context.caller.tenant, lease_id=params["approval_id"]
    )
    if lease is None or lease.get("kind") != ACTION_APPROVAL_KIND:
        raise LookupError("action approval is unavailable")
    return approval_lease_to_props(lease)


async def _decide_approval(context: Any, params: Mapping[str, Any], target: str) -> Any:
    approval_id = params["approval_id"]
    if not approval_id.startswith(APPROVAL_ID_PREFIX):
        raise ValueError("approval_id must identify an action approval")
    lease = await context.client.control_leases.get(
        tenant=context.caller.tenant, lease_id=approval_id
    )
    if lease is None or lease.get("kind") != ACTION_APPROVAL_KIND:
        raise LookupError("action approval is unavailable")
    if lease.get("status") != "active":
        raise LookupError("action approval is no longer pending")
    return await context.client.control_leases.transition(
        tenant=context.caller.tenant,
        lease_id=approval_id,
        expected_revision=lease["revision"],
        to=target,
        idempotency_key=f"decide:{approval_id}",
    )


async def grant_approval(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    return await _decide_approval(context, params, "consumed")


async def deny_approval(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    return await _decide_approval(context, params, "revoked")


async def _rbac_admin(context: Any, op: Mapping[str, Any]) -> Any:
    """One engine ``RbacAdmin`` write under the caller's verified authority."""
    from arbora.api.engine_errors import (
        ENGINE_ERRORS,  # noqa: F401 - refusal vocabulary
    )
    from arbora.pin_lag.medulla import send_main

    return await send_main(
        context, "RbacAdmin", {"op": dict(op)}, idempotent=True, models=("ShareGrant",)
    )


async def grant_share(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """``access.share.grant``: the engine decides; Arbora maps its refusals."""
    answer = await _rbac_admin(
        context,
        {
            "ShareGrant": {
                "node_id": params["node_id"],
                "scope": params["scope"],
                "grantees": list(params["grantees"]),
            }
        },
    )
    return {"value": answer if isinstance(answer, Mapping) else {}}


async def revoke_share(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    answer = await _rbac_admin(context, {"ShareRevoke": {"node_id": params["node_id"]}})
    return {"value": answer if isinstance(answer, Mapping) else {}}


async def plan_ownership(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """``access.ownership.plan``: the engine's read-only plan of one graph."""
    from arbora.api.invoke.bootstrap import caller_dispatch_graph
    from arbora.pin_lag.medulla import send_main

    graph = params.get("graph") or await caller_dispatch_graph(context)
    answer = await send_main(
        context,
        "GraphOwnershipPlan",
        {"graph": graph},
        models=("GraphOwnershipPlanReply",),
    )
    return {"value": answer if isinstance(answer, Mapping) else {}}


async def apply_ownership(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """``access.ownership.apply``: pass the plan digest and approval reference through."""
    answer = await _rbac_admin(
        context,
        {
            "ApplyOwnership": {
                "graph": params["graph"],
                "plan": list(params["plan"]),
                "plan_digest": params["plan_digest"],
                "signals_digest": params["signals_digest"],
            }
        },
    )
    return {"value": answer if isinstance(answer, Mapping) else {}}
