"""Typed ingestion-runner facade for the Arbora operation registry.

ARBORA-OPS-R020.1/R020.2: Arbora exposes ingest operations for repository
indexing, source sync, pack and job management, drift listing and repair,
and embedding admission (ARBORA-OPS-R020.3 through R020.5 remain), but
calls the ingestion SDK only through one typed runner port. An uncomposed
runner fails closed with a typed ``UNAVAILABLE`` refusal rather than an
opaque error or a silent no-op, matching the ``arbora.access``/
``arbora.fleet`` service layer convention: pure functions over the
verified operation context, params, and op spec, reusable across every
surface the invoke pipeline serves.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from arbora.api.invoke.pipeline import OperationRefused

IngestSyncMode = Literal["full", "incremental"]
IngestJobStatus = Literal["queued", "running", "completed", "failed"]


class IngestSourceState(StrEnum):
    """Whether an ingestion source or pack is syncing, held, or failing."""

    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"


class IngestSyncReceipt(BaseModel):
    """A durable record of one accepted source-sync request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    source_id: str
    tenant: str
    mode: IngestSyncMode
    status: IngestJobStatus
    #: Why a failed sync failed: the SDK reason code, never a payload
    #: (ARBORA-OPS-R020.6.8).
    reason: str | None = None
    #: The engine refusal code when ``reason`` is ``ingest_refused``.
    engine_code: str | None = None


class IngestIndexReceipt(BaseModel):
    """A durable record of one accepted repository-index request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    repo_id: str
    tenant: str
    status: IngestJobStatus


class IngestSourceRecord(BaseModel):
    """One ingestion source's identity and current state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_id: str
    tenant: str
    state: IngestSourceState
    connector: str | None = None
    record_type: str | None = None
    incremental: bool | None = None
    cursor: str | None = None
    records: int | None = None
    last_job_id: str | None = None
    sync_state: str | None = None
    sync_reason: str | None = None
    sync_engine_code: str | None = None


class IngestSourceInventory(BaseModel):
    """A tenant's full ingestion-source inventory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sources: tuple[IngestSourceRecord, ...]


class IngestPackRecord(BaseModel):
    """One ingestion pack's identity and current state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pack_id: str
    tenant: str
    state: IngestSourceState


class IngestPackInventory(BaseModel):
    """A tenant's full ingestion-pack inventory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    packs: tuple[IngestPackRecord, ...]


class IngestJobRecord(BaseModel):
    """One durable ingestion job's identity and current state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    tenant: str
    status: IngestJobStatus
    #: The failure cause the job recorded (ARBORA-OPS-R020.6.8).
    reason: str | None = None
    engine_code: str | None = None


IngestSubmissionKind = Literal[
    "ingest", "ingest_url", "ingest_knowledge_pack", "fact_extract", "distill"
]


class IngestSubmissionReceipt(BaseModel):
    """A durable record of one accepted ingest submission."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    tenant: str
    kind: IngestSubmissionKind
    status: IngestJobStatus


class IngestJobInventory(BaseModel):
    """One page of a tenant's durable ingestion jobs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    jobs: tuple[IngestJobRecord, ...]
    next_cursor: str | None = None


class IngestJobControlReceipt(BaseModel):
    """The outcome of one job control request (cancel, prioritize)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    job_id: str
    tenant: str
    action: Literal["cancel", "prioritize"]
    applied: bool


@runtime_checkable
class IngestRunner(Protocol):
    """The one typed port Arbora calls the ingestion SDK through.

    ARBORA-OPS-R020.3 through R020.5 add further port methods (pack/job
    management, drift repair, embedding admission) as their own slices land;
    this port now covers the R020.1 source-sync entry point and the R020.2
    repository-indexing and source-inventory entry points.
    """

    async def sync_source(
        self,
        *,
        tenant: str,
        source_id: str,
        mode: str,
        idempotency_key: str,
        caller: Any = None,
    ) -> IngestSyncReceipt: ...

    async def index_repository(
        self,
        *,
        tenant: str,
        repo_id: str,
        idempotency_key: str,
        max_working_bytes: int | None = None,
    ) -> IngestIndexReceipt: ...

    async def list_sources(
        self, *, tenant: str, caller: Any = None
    ) -> IngestSourceInventory: ...

    async def get_source_status(
        self, *, tenant: str, source_id: str, caller: Any = None
    ) -> IngestSourceRecord: ...

    async def list_packs(
        self, *, tenant: str, caller: Any = None
    ) -> IngestPackInventory: ...

    async def get_job_status(self, *, tenant: str, job_id: str) -> IngestJobRecord: ...

    async def submit_ingest(
        self,
        *,
        tenant: str,
        kind: str,
        target: str,
        options: Mapping[str, str],
        idempotency_key: str,
        caller: Any = None,
    ) -> IngestSubmissionReceipt: ...

    async def list_jobs(
        self,
        *,
        tenant: str,
        cursor: str | None = None,
        limit: int = 50,
        caller: Any = None,
    ) -> IngestJobInventory: ...

    async def cancel_job(
        self, *, tenant: str, job_id: str, caller: Any = None
    ) -> IngestJobControlReceipt: ...

    async def prioritize_job(
        self, *, tenant: str, job_id: str, bucket: int, caller: Any = None
    ) -> IngestJobControlReceipt: ...
    # ARBORA-OPS-R020.4: drift listing and repair-with-approval.
    async def list_drift(
        self, *, tenant: str, source_id: str | None = None
    ) -> Mapping[str, Any]: ...

    async def get_drift(self, *, tenant: str, drift_id: str) -> Mapping[str, Any]: ...

    async def propose_repair(
        self, *, tenant: str, drift_id: str, idempotency_key: str
    ) -> Mapping[str, Any]: ...

    async def get_repair(
        self, *, tenant: str, repair_id: str
    ) -> Mapping[str, Any] | None: ...

    async def apply_repair(
        self, *, tenant: str, repair_id: str, approval_ref: str, idempotency_key: str
    ) -> Mapping[str, Any]: ...

    # ARBORA-OPS-R020.5: embedding admission and re-embedding.
    async def admit_embedding(
        self, *, tenant: str, model: str, dimensions: int, idempotency_key: str
    ) -> Mapping[str, Any]: ...

    async def reembed(
        self, *, tenant: str, source_id: str, model: str, idempotency_key: str
    ) -> Mapping[str, Any]: ...


def _bound_runner(context: Any) -> IngestRunner:
    runner = context.services.get("ingest_runner")
    if runner is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "ingest runner is not composed"}
        )
    return runner


async def sync_source(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Request a durable source sync through the composed ingestion runner."""
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    runner = _bound_runner(context)
    receipt = await runner.sync_source(
        tenant=context.caller.tenant,
        source_id=params["source_id"],
        mode=params["mode"],
        idempotency_key=context.idempotency_key,
        caller=context.caller,
    )
    return {"value": receipt.model_dump(mode="json", exclude_none=True)}


#: The engine knob that bounds one index job's working bytes (MEDULLA-OBSERVE-R006.12).
INDEX_KNOB = "scheduler.index_max_working_bytes"
_KNOB_PAGE = 500
_KNOB_PAGES = 8


async def engine_index_knob(context: Any) -> int | None:
    """Read the engine's index working-bytes knob from ``Introspect`` section ``knobs``.

    PIN-LAG (remove after the medulla re-pin that carries ``IntrospectSection.knobs``): the
    pinned release lacks the method or the section, so the read answers ``None`` and the
    host grant alone bounds the tenant. The engine still refuses a job past its own knob.
    """
    from arbora.api.ops.engine_calls import engine_send
    from arbora.pin_lag.medulla import (
        PinLagUnavailable,
        require_enum_member,
        require_methods,
    )

    try:
        require_methods("Introspect")
        require_enum_member("IntrospectSection", "knobs")
    except PinLagUnavailable:
        return None
    cursor: str | None = None
    for _ in range(_KNOB_PAGES):
        params: dict[str, Any] = {"section": "knobs", "limit": _KNOB_PAGE}
        if cursor is not None:
            params["cursor"] = cursor
        page = await engine_send(context, "Introspect", params)
        for row in page.get("rows") or ():
            if row.get("key") == INDEX_KNOB:
                return int(row["value"])
        cursor = page.get("next_cursor")
        if not cursor:
            return None
    return None


async def resource_grant_bytes(context: Any) -> int:
    """The working bytes one tenant may hold at once: the host grant clamped to the engine knob.

    ARBORA-CAPACITY-R753: ``IndexRepository`` has no ``max_working_bytes`` request field;
    the engine knob ``scheduler.index_max_working_bytes`` bounds each job and is reserved
    per job. The knob is read through ``Introspect`` section ``knobs``
    (:func:`engine_index_knob`); a bound ``index_knob_reader`` service replaces that read.
    With no knob read (PIN-LAG) the host grant alone bounds the tenant.
    """
    host = int(context.services["config"].resource_grants_max_working_bytes)
    reader = context.services.get("index_knob_reader")
    knob = await reader() if reader is not None else await engine_index_knob(context)
    return min(host, int(knob)) if knob else host


async def index_repository(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Request a durable repository index through the composed ingestion runner."""
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    runner = _bound_runner(context)
    repo_id = params["repo_id"]
    grant = params.get("max_working_bytes")
    if grant is None:
        receipt = await runner.index_repository(
            tenant=context.caller.tenant,
            repo_id=repo_id,
            idempotency_key=context.idempotency_key,
        )
        return {"value": receipt.model_dump(mode="json")}
    from arbora.api.ops.resource_grants import ResourceGrantLedger

    ledger = ResourceGrantLedger(context, limit=await resource_grant_bytes(context))
    job_key = f"{repo_id}:{context.idempotency_key}"
    try:
        await ledger.admit(job_key, int(grant))
        receipt = await runner.index_repository(
            tenant=context.caller.tenant,
            repo_id=repo_id,
            idempotency_key=context.idempotency_key,
            max_working_bytes=int(grant),
        )
    finally:
        # The grant is released on success, cancellation and failure alike,
        # including a failed admission: the engine accounts the running job.
        await ledger.release(job_key)
    return {"value": receipt.model_dump(mode="json")}


async def list_sources(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """List this tenant's ingestion sources through the composed runner."""
    runner = _bound_runner(context)
    inventory = await runner.list_sources(
        tenant=context.caller.tenant, caller=context.caller
    )
    return {"value": inventory.model_dump(mode="json", exclude_none=True)}


async def get_source_status(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Show one ingestion source's current status through the composed runner."""
    runner = _bound_runner(context)
    record = await runner.get_source_status(
        tenant=context.caller.tenant,
        source_id=params["source_id"],
        caller=context.caller,
    )
    return {"value": record.model_dump(mode="json", exclude_none=True)}


async def list_packs(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """List this tenant's ingestion packs through the composed runner."""
    runner = _bound_runner(context)
    inventory = await runner.list_packs(
        tenant=context.caller.tenant, caller=context.caller
    )
    return {"value": inventory.model_dump(mode="json")}


async def get_job_status(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Report one durable ingestion job's state through the composed runner."""
    runner = _bound_runner(context)
    record = await runner.get_job_status(
        tenant=context.caller.tenant, job_id=params["job_id"]
    )
    return {"value": record.model_dump(mode="json", exclude_none=True)}


async def submit_ingest(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Submit one ingest, URL, pack, fact-extract or distill job (ARBORA-HOST-R005.2.3.2)."""
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    runner = _bound_runner(context)
    receipt = await runner.submit_ingest(
        tenant=context.caller.tenant,
        kind=params["kind"],
        target=params["target"],
        options=dict(params.get("options") or {}),
        idempotency_key=context.idempotency_key,
        caller=context.caller,
    )
    return {"value": receipt.model_dump(mode="json")}


async def list_jobs(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """List this tenant's durable ingestion jobs (ARBORA-HOST-R005.2.3.4)."""
    runner = _bound_runner(context)
    page = await runner.list_jobs(
        tenant=context.caller.tenant,
        cursor=params.get("cursor"),
        limit=params.get("limit", 50),
        caller=context.caller,
    )
    return {"value": page.model_dump(mode="json", exclude_none=True)}


async def cancel_job(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Cancel one pending ingestion job (ARBORA-HOST-R005.2.3.4)."""
    runner = _bound_runner(context)
    receipt = await runner.cancel_job(
        tenant=context.caller.tenant, job_id=params["job_id"], caller=context.caller
    )
    return {"value": receipt.model_dump(mode="json")}


async def prioritize_job(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Re-prioritize one ingestion job (ARBORA-HOST-R005.2.3.4)."""
    runner = _bound_runner(context)
    receipt = await runner.prioritize_job(
        tenant=context.caller.tenant,
        job_id=params["job_id"],
        bucket=params["bucket"],
        caller=context.caller,
    )
    return {"value": receipt.model_dump(mode="json")}


def _require_key(context: Any) -> str:
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    return str(context.idempotency_key)


async def list_drift(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """List this tenant's detected source drift through the composed runner."""
    runner = _bound_runner(context)
    found = await runner.list_drift(
        tenant=context.caller.tenant, source_id=params.get("source_id")
    )
    return {"value": dict(found)}


async def get_drift(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Show one drift record through the composed runner."""
    runner = _bound_runner(context)
    found = await runner.get_drift(
        tenant=context.caller.tenant, drift_id=params["drift_id"]
    )
    return {"value": dict(found)}


async def propose_repair(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Record a repair proposal with its preview; nothing is applied."""
    key = _require_key(context)
    runner = _bound_runner(context)
    proposal = await runner.propose_repair(
        tenant=context.caller.tenant,
        drift_id=params["drift_id"],
        idempotency_key=key,
    )
    return {"value": dict(proposal)}


async def approve_repair(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Apply a previewed repair, but only against an approval reference."""
    key = _require_key(context)
    runner = _bound_runner(context)
    proposal = await runner.get_repair(
        tenant=context.caller.tenant, repair_id=params["repair_id"]
    )
    if proposal is None:
        raise LookupError("repair proposal not found")
    if not proposal.get("preview"):
        raise OperationRefused(
            "PRECONDITION_FAILED", {"reason": "repair has no recorded preview"}
        )
    applied = await runner.apply_repair(
        tenant=context.caller.tenant,
        repair_id=params["repair_id"],
        approval_ref=params["approval_ref"],
        idempotency_key=key,
    )
    return {"value": dict(applied)}


async def admit_embedding(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Admit an embedding model/dimension through the composed runner."""
    key = _require_key(context)
    runner = _bound_runner(context)
    receipt = await runner.admit_embedding(
        tenant=context.caller.tenant,
        model=params["model"],
        dimensions=params["dimensions"],
        idempotency_key=key,
    )
    return {"value": dict(receipt)}


async def reembed(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Request a durable re-embedding job through the composed runner."""
    key = _require_key(context)
    runner = _bound_runner(context)
    receipt = await runner.reembed(
        tenant=context.caller.tenant,
        source_id=params["source_id"],
        model=params["model"],
        idempotency_key=key,
    )
    return {"value": dict(receipt)}
