"""Application admission and connector retirement (ARBORA-DATA-MARKET-R004).

``apps.admit`` opens one :class:`AdmissionRun` (a dry run with ``dry_run``), takes it as
far as its governed approval and probes allow, and rolls it back on a failed probe or a
failed parity check without touching the prior connector. ``apps.retire_connector``
retires the prior connector only from ``admitted`` with parity passed and its own
approval. Both are ``Confirm.PLAN``. Arbora holds no run state: every read and write is
an engine node or an engine ``GovernedChange`` (``arbora.control_plane.admission.engine``).
The tenant is the verified caller's and is never a parameter.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.pin_guards import guard
from arbora.api.registry import Confirm, OpSpec
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

SCOPE = "federation:admin"
#: The run node and the governed change replay a retried request by their own ids.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "apps.admit": "medulla:CreateNodeIfAbsent",
    "apps.retire_connector": "medulla:GovernedChange",
}
_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,255}$"


class AppsAdmitParams(Params):
    app_name: str = Field(pattern=_NAME)
    source_id: str = Field(pattern=_NAME)
    source_dialect: str = Field(min_length=1, max_length=256)
    source_version: str = Field(min_length=1, max_length=256)
    tables: list[str] = Field(min_length=1, max_length=256)
    prior_connector: str | None = Field(default=None, pattern=_NAME)
    credentials_scope: list[str] = Field(default_factory=list, max_length=64)
    tenant_scope: list[str] = Field(default_factory=list, max_length=64)
    mapping_approved: bool = False
    conformance_checked: bool = False
    rollback_proven: bool = False
    connector_overlap: list[str] = Field(default_factory=list, max_length=64)
    approve: bool = False
    dry_run: bool = False


class AppsRetireParams(Params):
    run_id: str = Field(min_length=1, max_length=512)
    approve: bool = False


class AppsResult(Params):
    value: dict[str, Any]


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


async def handle_apps_admit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    tenant = str(context.caller.tenant)
    intent = _intent(tenant, params)
    if params["dry_run"]:
        plan = plan_admission(intent)
        return {
            "value": {
                "plan": plan.model_dump(mode="json"),
                "plan_digest": plan_digest(
                    intent, params["source_id"], params["tables"]
                ),
                "source_id": params["source_id"],
                "prior_connector": params["prior_connector"],
                "parity_tables": list(params["tables"]),
            }
        }
    run = _run(context)
    try:
        opened = await run.open(
            tenant,
            intent,
            source_id=params["source_id"],
            prior_connector=params["prior_connector"],
            tables=params["tables"],
            fence=str(context.idempotency_key or ""),
        )
        view = await run.advance(tenant, opened["run_id"], approve=params["approve"])
    except AdmissionError as error:
        raise _refused(error) from error
    return {"value": view}


async def handle_apps_retire_connector(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    try:
        view = await _run(context).retire(
            str(context.caller.tenant), params["run_id"], approve=params["approve"]
        )
    except AdmissionError as error:
        raise _refused(error) from error
    return {"value": view}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="apps.admit",
            summary="Admit one application source: plan, approve, probe, admit or roll back",
            examples=("admit this application source",),
            params=AppsAdmitParams,
            result=AppsResult,
            handler="arbora.api.ops.apps.handle_apps_admit",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
        build_write_op(
            op_id="apps.retire_connector",
            summary="Retire the prior connector of an admitted application source",
            examples=("retire the old connector for this admitted source",),
            params=AppsRetireParams,
            result=AppsResult,
            handler="arbora.api.ops.apps.handle_apps_retire_connector",
            scope=SCOPE,
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "AppsAdmitParams",
    "AppsRetireParams",
    "handle_apps_admit",
    "handle_apps_retire_connector",
    "operations",
    "specs",
]
