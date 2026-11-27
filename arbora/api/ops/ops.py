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
from arbora.api.registry import Confirm, Effect, OpSpec
from arbora.observability.state import HealthState


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


def _ranking_probe(health: HealthState | None) -> dict[str, Any] | None:
    """The host's ranking probe result, or ``None`` when no health state is bound."""
    from arbora.observability.runtime_health import run_bound_probe

    if health is None:
        return None
    return run_bound_probe(health, "ranking")


def _with_ranking_check(
    health: HealthState | None, checks: Mapping[str, Any]
) -> dict[str, Any]:
    """The report's checks, plus which order ``find`` serves and why (never silent)."""
    ranking = _ranking_probe(health)
    if ranking is None:
        return dict(checks)
    summary = ranking.get("detail", {}).get("summary") or "ranking: unknown"
    return {**checks, "ranking": summary}


async def handle_ops_doctor(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return one diagnostic report scoped to the caller's own tenant."""
    store = _bound_store(context)
    report = await store.doctor(tenant=context.caller.tenant)
    value = report.model_dump(mode="json")
    from arbora.security.anonymous import indicator

    unsecured = indicator(context.services["config"])
    if unsecured:
        # The unsecured mode is named in every report, beside the store's own checks.
        value["checks"] = {**value["checks"], "auth": unsecured}
    from arbora.pin_lag.gates import gate_checks

    # A gate the pinned engine cannot serve is named in every report (PIN-LAG), and so
    # is the ranking floor.
    checks = _with_ranking_check(context.services.get("health"), value["checks"])
    from arbora.api.ops.operator_audit import operator_audit_check

    value["checks"] = {
        **checks,
        **gate_checks(),
        "operator_audit": operator_audit_check(),
    }
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


class OpsHooksParams(Params):
    #: The agent surfaces to change; ``None`` is every supported one.
    agents: list[str] | None = Field(default=None, max_length=64)


class OpsHooksResult(Params):
    value: dict[str, Any]


async def _hooks(
    context: Any, params: Mapping[str, Any], *, install: bool
) -> dict[str, Any]:
    """Run the managed-block installer; it is idempotent and touches only its block."""
    from arbora.ecosystem.hook_installer import HookInstaller

    installer = HookInstaller()
    agents = params.get("agents")
    results = installer.install(agents) if install else installer.uninstall(agents)
    return {"value": {"results": dict(results)}}


async def hooks_install(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _hooks(context, params, install=True)


async def hooks_uninstall(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _hooks(context, params, install=False)


PROCESS_QUESTION = "process_remediation"
#: The engine family's own refusals (MEDULLA-DECISION-ENGINE-R820).
PROCESS_REFUSALS = frozenset({"LIVENESS_EVIDENCE_STALE", "LIVENESS_EVIDENCE_MISMATCH"})
ProcessVerdict = Literal["zombie", "hung", "progressing"]
_PROCESS_VERDICTS: dict[str, ProcessVerdict] = {
    "zombie": "zombie",
    "hung": "hung",
    "progressing": "progressing",
}


class LivenessEvidence(Params):
    """The harness's run-bound liveness evidence for one process."""

    evidence_run_id: str = Field(min_length=1, max_length=256)
    observed_at_ms: int = Field(ge=0)
    last_progress_ms: int = Field(ge=0)
    cpu_time_ms: int = Field(ge=0)
    output_bytes: int = Field(ge=0)
    holds_child_output: bool


class ProcessProposeParams(Params):
    run_id: str = Field(min_length=1, max_length=256)
    process_id: str = Field(min_length=1, max_length=256)
    evidence: LivenessEvidence


class ProcessTerminateParams(ProcessProposeParams):
    #: The digest the approved plan binds; it must equal the evidence's own digest.
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProcessProposal(Params):
    run_id: str
    process_id: str
    evidence_digest: str
    verdict: ProcessVerdict


class ProcessProposeResult(Params):
    value: ProcessProposal


class ProcessTerminateResult(Params):
    value: dict[str, Any]


@runtime_checkable
class ProcessTerminator(Protocol):
    """The harness termination of one run's process (MERISTEM-HARNESS-CYCLE-R063)."""

    async def terminate(
        self, *, run_id: str, process_id: str, approval_ref: str
    ) -> None: ...


def liveness_digest(params: Mapping[str, Any]) -> str:
    """SHA-256 over the run, process and liveness evidence the proposal is about."""
    import hashlib
    import json

    body = {
        "run_id": params["run_id"],
        "process_id": params["process_id"],
        "evidence": dict(params["evidence"]),
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


async def _process_verdict(context: Any, params: Mapping[str, Any]) -> ProcessVerdict:
    """Ask the engine whether the evidence shows a zombie, a hung or a live process."""
    from medulla import decision_client as dc

    from arbora.api.ops.plans import _feature_schema
    from arbora.api.ops.question_round import (
        FamilyRefusal,
        chosen_option,
        decide_question,
        question_request,
    )

    evidence = params["evidence"]
    request = question_request(
        str(context.caller.tenant),
        f"ops.process:{params['run_id']}:{params['process_id']}",
        PROCESS_QUESTION,
        dc.declared([dc.DeclaredOption("remediation")]),
        await _feature_schema(context, str(context.caller.tenant)),
        [
            dc.param("run_id", "text", params["run_id"]),
            dc.param("evidence_run_id", "text", evidence["evidence_run_id"]),
            dc.param("observed_at_ms", "int", evidence["observed_at_ms"]),
            dc.param("last_progress_ms", "int", evidence["last_progress_ms"]),
            dc.param("cpu_time_ms", "int", evidence["cpu_time_ms"]),
            dc.param("output_bytes", "int", evidence["output_bytes"]),
            dc.param("holds_child_output", "bool", evidence["holds_child_output"]),
        ],
    )
    try:
        record = await decide_question(
            context, PROCESS_QUESTION, request, family_codes=PROCESS_REFUSALS
        )
    except FamilyRefusal as refused:
        from arbora.api.invoke.pipeline import OperationRefused

        raise OperationRefused(
            "POLICY_DENIED", {"engine_code": refused.code}
        ) from refused
    verdict = _PROCESS_VERDICTS.get(chosen_option(record))
    if verdict is None:
        from arbora.api.invoke.pipeline import OperationRefused

        raise OperationRefused("RECEIPT_MISMATCH", {"reason": "unknown verdict"})
    return verdict


async def handle_process_propose(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return the engine's verdict on one process as a proposal; nothing is stopped."""
    verdict = await _process_verdict(context, params)
    proposal = ProcessProposal(
        run_id=params["run_id"],
        process_id=params["process_id"],
        evidence_digest=liveness_digest(params),
        verdict=verdict,
    )
    return {"value": proposal.model_dump(mode="json")}


async def handle_process_terminate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Apply one termination the approval binds to this run, process and evidence."""
    from arbora.api.invoke.pipeline import OperationRefused

    terminator: ProcessTerminator = bound_service(
        context, "process_terminator", reason="process terminator is not composed"
    )
    if liveness_digest(params) != params["evidence_digest"]:
        raise OperationRefused("POLICY_DENIED", {"reason": "evidence_mismatch"})
    verdict = await _process_verdict(context, params)
    if verdict == "progressing":
        raise OperationRefused("POLICY_DENIED", {"reason": "process_is_progressing"})
    await terminator.terminate(
        run_id=params["run_id"],
        process_id=params["process_id"],
        approval_ref=params["evidence_digest"],
    )
    return {
        "value": {
            "run_id": params["run_id"],
            "process_id": params["process_id"],
            "evidence_digest": params["evidence_digest"],
            "verdict": verdict,
            "terminated": True,
        }
    }


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
        build_read_op(
            op_id="ops.process.propose",
            summary="Propose whether a run's process is a zombie, hung or progressing",
            examples=("is this run's process stuck",),
            params=ProcessProposeParams,
            result=ProcessProposeResult,
            handler="arbora.api.ops.ops.handle_process_propose",
            scope="ops:read",
        ),
        build_write_op(
            op_id="ops.process.terminate",
            summary="Terminate one run's process under an approval bound to its evidence",
            examples=("stop this zombie process",),
            params=ProcessTerminateParams,
            result=ProcessTerminateResult,
            handler="arbora.api.ops.ops.handle_process_terminate",
            scope="ops:admin",
            effect=Effect.ADMIN,
            confirm=Confirm.PLAN,
        ),
        build_write_op(
            op_id="ops.hooks.install",
            summary="Install the managed memory hook block for the agent surfaces",
            examples=("install the agent hooks",),
            params=OpsHooksParams,
            result=OpsHooksResult,
            handler="arbora.api.ops.ops.hooks_install",
            scope="ops:admin",
            effect=Effect.ADMIN,
        ),
        build_write_op(
            op_id="ops.hooks.uninstall",
            summary="Remove the managed memory hook block from the agent surfaces",
            examples=("remove the agent hooks",),
            params=OpsHooksParams,
            result=OpsHooksResult,
            handler="arbora.api.ops.ops.hooks_uninstall",
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
    "ProcessProposal",
    "ProcessTerminator",
    "handle_process_propose",
    "handle_process_terminate",
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
