"""The application-admission port the ``apps.*`` operations call (ARBORA-DATA-MARKET-R004)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.pin_guards import guard
from arbora.control_plane.admission import (
    AdmissionError,
    AdmissionIntent,
    AdmissionRun,
    plan_admission,
)
from arbora.control_plane.admission.engine import (
    SOURCE_METHODS,
    EngineAdmissionGovernance,
    EngineAdmissionSources,
    EngineAdmissionStore,
    parity_sample,
)
from arbora.control_plane.admission.run import plan_digest

__all__ = ["ControlPlaneAppAdmission"]


def _intent(tenant: str, params: Mapping[str, Any]) -> AdmissionIntent:
    return AdmissionIntent(
        app_name=params["app_name"],
        tenant_id=tenant,
        source_dialect=params["source_dialect"],
        source_version=params["source_version"],
        credentials_scope=tuple(params["credentials_scope"]),
        tenant_scope=tuple(params["tenant_scope"]),
        mapping_approved=params["mapping_approved"],
        conformance_checked=params["conformance_checked"],
        rollback_proven=params["rollback_proven"],
        connector_overlap=tuple(params["connector_overlap"]),
    )


def _run(context: Any) -> AdmissionRun:
    """The run over the verified caller's engine, or the host-bound one."""
    bound = context.services.get("admission_run") if context.services else None
    if bound is not None:
        return bound

    async def send(method: str, params: dict[str, Any]) -> Any:
        if method in SOURCE_METHODS:
            guard(method)
        return await engine_send(context, method, params)

    return AdmissionRun(
        EngineAdmissionStore(send),
        EngineAdmissionGovernance(send),
        EngineAdmissionSources(send),
        parity_sample=parity_sample(),
    )


def _refused(error: AdmissionError) -> OperationRefused:
    return OperationRefused(error.code, error.details)


class ControlPlaneAppAdmission:
    """``AppAdmission`` over the control-plane admission run; a refusal is the served code."""

    def plan(self, tenant: str, params: Mapping[str, Any]) -> dict[str, Any]:
        intent = _intent(tenant, params)
        return {
            "plan": plan_admission(intent).model_dump(mode="json"),
            "plan_digest": plan_digest(intent, params["source_id"], params["tables"]),
            "source_id": params["source_id"],
            "prior_connector": params["prior_connector"],
            "parity_tables": list(params["tables"]),
        }

    async def admit(
        self, context: Any, params: Mapping[str, Any], *, fence: str
    ) -> dict[str, Any]:
        tenant = str(context.caller.tenant)
        run = _run(context)
        try:
            opened = await run.open(
                tenant,
                _intent(tenant, params),
                source_id=params["source_id"],
                prior_connector=params["prior_connector"],
                tables=params["tables"],
                fence=fence,
            )
            return await run.advance(
                tenant, opened["run_id"], approve=params["approve"]
            )
        except AdmissionError as error:
            raise _refused(error) from error

    async def retire(
        self, context: Any, run_id: str, *, approve: bool
    ) -> dict[str, Any]:
        try:
            return await _run(context).retire(
                str(context.caller.tenant), run_id, approve=approve
            )
        except AdmissionError as error:
            raise _refused(error) from error
