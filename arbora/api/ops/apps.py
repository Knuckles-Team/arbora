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
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.ops._common import Params, build_write_op
from arbora.api.ops.ports import port_of
from arbora.api.registry import Confirm, OpSpec

SCOPE = "federation:admin"
#: The run node and the governed change replay a retried request by their own ids.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "apps.admit": "medulla:CreateNodeIfAbsent",
        "apps.retire_connector": "medulla:GovernedChange",
    }
)
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


async def handle_apps_admit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    admission = port_of(context, "apps")
    if params["dry_run"]:
        return {"value": admission.plan(str(context.caller.tenant), params)}
    view = await admission.admit(
        context, params, fence=str(context.idempotency_key or "")
    )
    return {"value": view}


async def handle_apps_retire_connector(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    view = await port_of(context, "apps").retire(
        context, params["run_id"], approve=params["approve"]
    )
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
