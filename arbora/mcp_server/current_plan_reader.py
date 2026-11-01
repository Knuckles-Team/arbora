"""Project native current-plan reads under the admitted caller's authority.

This host adapter owns no plan state and retains no caller, client or session.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from scion_sdk.current_plan import (
    CurrentPlanRead,
    CurrentPlanSnapshot,
    PlanItem,
)

from arbora.api.invoke.bootstrap import _caller_dispatch_graph
from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import claim_values
from arbora.api.ops.runs import UNAVAILABLE
from arbora.security.anonymous import client_scope


async def _native_read(client: Any, *, run_id: str, tenant: str, graph: str) -> Any:
    # Older published EG distributions cannot provide this projection yet.
    try:
        from epistemic_graph.generated.coordination import (
            decode_get_current_plan,
            send_get_current_plan,
        )
    except ImportError:
        return None
    from epistemic_graph import EngineResponseError
    from epistemic_graph.generated import ContractViolation

    from arbora.api.errors import EngineRefusal

    try:
        result = await send_get_current_plan(
            client, {"run_id": run_id, "tenant": tenant}, graph
        )
    except EngineResponseError as exc:
        raise EngineRefusal(exc.code) from exc
    try:
        snapshot = decode_get_current_plan(result)
    except (ContractViolation, ValueError):
        return None
    return None if snapshot is None else snapshot.model_dump(mode="json")


async def _native_owner_graph(
    client: Any, *, run_id: str, tenant: str, principal: str
) -> str | None:
    """Resolve only the caller-owned native admission on the host's WorkItem graph."""
    from epistemic_graph import EngineResponseError
    from epistemic_graph.generated import ContractViolation

    from arbora.api.errors import EngineRefusal
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

    try:
        from epistemic_graph.generated.coordination import (
            decode_get_run_work_item,
            send_get_run_work_item,
        )
    except ImportError:
        return None
    try:
        result = await send_get_run_work_item(
            client, {"run_id": run_id, "tenant": tenant}, CONTROL_GRAPH
        )
    except EngineResponseError as exc:
        raise EngineRefusal(exc.code) from exc
    try:
        view = decode_get_run_work_item(result)
    except (ContractViolation, ValueError):
        return None
    return _owned_admission_graph(
        view, run_id=run_id, tenant=tenant, principal=principal
    )


def _owned_admission_graph(
    view: Any, *, run_id: str, tenant: str, principal: str
) -> str | None:
    """Validate the decoded native admission without changing caller authority."""
    from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

    if view is None:
        return None
    admission = view.admission
    owner = admission.context
    if (
        admission.run_id != run_id
        or admission.work_item_id is not None
        or owner.tenant_id != tenant
        or owner.subject_id != principal
        or owner.graph != CONTROL_GRAPH
        or not view.work_item.work_item_id.strip()
    ):
        return None
    return owner.graph


def _utc_millis(value: Any) -> datetime:
    if type(value) is not int or value < 0:
        raise ValueError("invalid snapshot time")
    return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)


def _project(raw: Any) -> CurrentPlanRead:
    if raw is None:
        return UNAVAILABLE
    try:
        if not isinstance(raw, dict) or set(raw) != {
            "run_id",
            "revision",
            "observed_at_ms",
            "expires_at_ms",
            "items",
            "task_plan",
        }:
            return UNAVAILABLE
        if not isinstance(raw["items"], list):
            return UNAVAILABLE
        snapshot = CurrentPlanSnapshot(
            run_id=raw["run_id"],
            revision=raw["revision"],
            observed_at=_utc_millis(raw["observed_at_ms"]),
            expires_at=_utc_millis(raw["expires_at_ms"]),
            items=tuple(PlanItem.model_validate(item) for item in raw["items"]),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return UNAVAILABLE
    return CurrentPlanRead(availability="available", snapshot=snapshot)


async def read_native_snapshot(context: ExecutionContext, run_id: str) -> Any:
    """Read the native snapshot without widening the current caller's authority."""
    await _caller_dispatch_graph(context)
    if "kg:read" not in context.caller.effective_scopes:
        raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": ["kg:read"]})
    with client_scope(context.client, claim_values(context.caller.engine_claims)):
        # The tenant's content graph is not the native WorkItem owner graph.
        # Keep caller claims unchanged; EG's owner-bound read proves admission
        # before any plan payload is requested. Never adopt metadata routing.
        graph = await _native_owner_graph(
            context.client,
            run_id=run_id,
            tenant=context.caller.tenant,
            principal=context.caller.principal,
        )
        if graph is None:
            return None
        await _caller_dispatch_graph(context)
        return await _native_read(
            context.client, run_id=run_id, tenant=context.caller.tenant, graph=graph
        )


class NativeCurrentPlanReader:
    """One stateless reader shared by the serving root, bound afresh per call."""

    async def read(self, *, run_id: str, context: ExecutionContext) -> CurrentPlanRead:
        return _project(await read_native_snapshot(context, run_id))
