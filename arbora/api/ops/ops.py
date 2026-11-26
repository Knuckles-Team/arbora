"""Operational admin operations (ARBORA-OPS-R024.5, admin slice).

Implements the admin slice of ``ARBORA-OPS-R024``: ``ops.doctor``,
``ops.config``, ``ops.tenants``, ``ops.backup``, and ``ops.restore`` over
the hosted operational-admin store, bound by the caller's own authority and
scoped to the caller's own tenant, through one typed port, matching the
port convention in :mod:`arbora.api.ops.ingest`. An uncomposed
store fails closed with a typed ``UNAVAILABLE`` refusal rather than
fabricating a result. Graph/query/search/ontology administration is out of
scope here; it is owned by ``ARBORA-HOST-R026.x``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from arbora.api.ops._common import (
    FrozenState,
    Params,
    TenantState,
    bound_service,
    build_read_op,
    build_write_op,
)
from arbora.api.registry import Effect, OpSpec


class OpsDoctorParams(Params):
    pass


ConfigView = Literal["values", "groups", "schema", "secret_status"]


class OpsConfigParams(Params):
    view: ConfigView = "values"


class OpsConfigSetParams(Params):
    key: str = Field(min_length=1, max_length=256)
    value: Any = None


class ConfigChange(TenantState):
    """The settings after one change, and whether the process must restart."""

    settings: dict[str, Any] = Field(default_factory=dict)
    restart_required: bool = False


class OpsConfigSetResult(Params):
    value: ConfigChange


class OpsTenantsParams(Params):
    cursor: str | None = None
    limit: int = Field(default=50, ge=1, le=200)


class OpsBackupParams(Params):
    label: str = Field(min_length=1, max_length=256)


class OpsRestoreParams(Params):
    backup_id: str = Field(min_length=1, max_length=256)


class DoctorReport(TenantState):
    """One tenant-scoped diagnostic report; the store owns its exact shape."""

    status: str
    checks: dict[str, Any] = Field(default_factory=dict)


class OperationalConfig(TenantState):
    """One tenant-scoped operational config snapshot."""

    settings: dict[str, Any] = Field(default_factory=dict)


class TenantPage(FrozenState):
    """One cursor-paged slice of tenants visible to the caller's own authority."""

    tenants: tuple[str, ...] = ()
    next_cursor: str | None = None


class _TenantReceipt(TenantState):
    """Shared shape for the store's durable backup/restore receipts."""

    backup_id: str
    status: str


class BackupReceipt(_TenantReceipt):
    """One durable backup receipt; the store owns its exact shape."""


class RestoreReceipt(_TenantReceipt):
    """One durable restore receipt; the store owns its exact shape."""


class OpsDoctorResult(Params):
    value: DoctorReport


class OpsConfigResult(Params):
    value: OperationalConfig


class OpsTenantsResult(Params):
    value: TenantPage


class OpsBackupResult(Params):
    value: BackupReceipt


class OpsRestoreResult(Params):
    value: RestoreReceipt


@runtime_checkable
class OpsAdminStore(Protocol):
    """The one typed port Arbora calls the operational-admin store through."""

    async def doctor(self, *, tenant: str) -> DoctorReport: ...

    async def get_config(self, *, tenant: str) -> OperationalConfig: ...

    async def config_view(self, *, tenant: str, view: str) -> dict[str, Any]: ...

    async def set_config(
        self, *, tenant: str, key: str, value: Any, idempotency_key: str
    ) -> ConfigChange:
        """Apply one setting; raise ``ConfigRefused`` for an unknown key or value."""

    async def list_tenants(
        self, *, tenant: str, cursor: str | None, limit: int
    ) -> TenantPage: ...

    async def backup(
        self, *, tenant: str, label: str, idempotency_key: str
    ) -> BackupReceipt: ...

    async def restore(
        self, *, tenant: str, backup_id: str, idempotency_key: str
    ) -> RestoreReceipt: ...


class ConfigRefused(Exception):
    """The store refused a setting: ``UNKNOWN_SETTING`` or ``INVALID_VALUE``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def secret_status(rows: Any) -> list[dict[str, Any]]:
    """Presence only: every row is cut to ``{name, present}``, never a value."""
    return [
        {"name": str(row["name"]), "present": bool(row["present"])}
        for row in (rows or ())
    ]


def _bound_store(context: Any) -> OpsAdminStore:
    return bound_service(
        context, "ops_admin_store", reason="operational-admin store is not composed"
    )


async def handle_ops_doctor(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one diagnostic report scoped to the caller's own tenant."""
    store = _bound_store(context)
    report = await store.doctor(tenant=context.caller.tenant)
    value = report.model_dump(mode="json")
    from arbora.security.anonymous import indicator

    unsecured = indicator()
    if unsecured:
        # The unsecured mode is named in every report, beside the store's own checks.
        value["checks"] = {**value["checks"], "auth": unsecured}
    return {"value": value}


async def handle_ops_config(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the effective operational config scoped to the caller's own tenant."""
    store = _bound_store(context)
    view = params.get("view", "values")
    if view == "values":
        config = await store.get_config(tenant=context.caller.tenant)
        return {"value": config.model_dump(mode="json")}
    answer = await store.config_view(tenant=context.caller.tenant, view=view)
    if view == "secret_status":
        return {"value": {"secrets": secret_status(answer.get("secrets"))}}
    return {"value": answer}


async def handle_ops_config_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Apply one setting under the admin step-up; the store validates the key."""
    from arbora.api.invoke.pipeline import OperationRefused

    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    store = _bound_store(context)
    try:
        change = await store.set_config(
            tenant=context.caller.tenant,
            key=params["key"],
            value=params.get("value"),
            idempotency_key=context.idempotency_key,
        )
    except ConfigRefused as refused:
        raise OperationRefused(refused.code) from refused
    return {"value": change.model_dump(mode="json")}


async def handle_ops_tenants(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one cursor-paged slice of tenants scoped to the caller's own authority."""
    store = _bound_store(context)
    page = await store.list_tenants(
        tenant=context.caller.tenant,
        cursor=params.get("cursor"),
        limit=params["limit"],
    )
    return {"value": page.model_dump(mode="json")}


async def handle_ops_backup(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Reserve one durable backup scoped to the caller's own tenant."""
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    store = _bound_store(context)
    receipt = await store.backup(
        tenant=context.caller.tenant,
        label=params["label"],
        idempotency_key=context.idempotency_key,
    )
    return {"value": receipt.model_dump(mode="json")}


async def handle_ops_restore(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Restore one durable backup scoped to the caller's own tenant."""
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    store = _bound_store(context)
    receipt = await store.restore(
        tenant=context.caller.tenant,
        backup_id=params["backup_id"],
        idempotency_key=context.idempotency_key,
    )
    return {"value": receipt.model_dump(mode="json")}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_read_op(
            op_id="ops.doctor",
            summary="Run operational diagnostics scoped to the caller's own tenant",
            examples=("run a diagnostic check on this tenant",),
            params=OpsDoctorParams,
            result=OpsDoctorResult,
            handler="arbora.api.ops.ops.handle_ops_doctor",
            scope="ops:read",
        ),
        build_read_op(
            op_id="ops.config",
            summary="Read the effective operational config for the caller's own tenant",
            examples=("show the current operational config",),
            params=OpsConfigParams,
            result=OpsConfigResult,
            handler="arbora.api.ops.ops.handle_ops_config",
            scope="ops:read",
        ),
        build_write_op(
            op_id="ops.config.set",
            summary="Change one operational setting for the caller's own tenant",
            examples=("change an operational setting",),
            params=OpsConfigSetParams,
            result=OpsConfigSetResult,
            handler="arbora.api.ops.ops.handle_ops_config_set",
            scope="ops:admin",
            effect=Effect.ADMIN,
        ),
        build_read_op(
            op_id="ops.tenants",
            summary="List tenants visible under the caller's own admin authority",
            examples=("list the tenants I can administer",),
            params=OpsTenantsParams,
            result=OpsTenantsResult,
            handler="arbora.api.ops.ops.handle_ops_tenants",
            scope="ops:read",
        ),
        build_write_op(
            op_id="ops.backup",
            summary="Reserve a durable backup scoped to the caller's own tenant",
            examples=("back up this tenant now",),
            params=OpsBackupParams,
            result=OpsBackupResult,
            handler="arbora.api.ops.ops.handle_ops_backup",
            scope="ops:admin",
            effect=Effect.ADMIN,
        ),
        build_write_op(
            op_id="ops.restore",
            summary="Restore a durable backup scoped to the caller's own tenant",
            examples=("restore this tenant from a backup",),
            params=OpsRestoreParams,
            result=OpsRestoreResult,
            handler="arbora.api.ops.ops.handle_ops_restore",
            scope="ops:admin",
            effect=Effect.ADMIN,
        ),
    )


specs = operations

__all__ = [
    "BackupReceipt",
    "DoctorReport",
    "OperationalConfig",
    "OpsAdminStore",
    "OpsBackupParams",
    "OpsBackupResult",
    "ConfigChange",
    "ConfigRefused",
    "OpsConfigParams",
    "OpsConfigSetParams",
    "OpsConfigSetResult",
    "OpsConfigResult",
    "OpsDoctorParams",
    "OpsDoctorResult",
    "OpsRestoreParams",
    "OpsRestoreResult",
    "OpsTenantsParams",
    "OpsTenantsResult",
    "RestoreReceipt",
    "TenantPage",
    "handle_ops_backup",
    "handle_ops_config",
    "handle_ops_config_set",
    "handle_ops_doctor",
    "handle_ops_restore",
    "handle_ops_tenants",
    "operations",
    "specs",
]
