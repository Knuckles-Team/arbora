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
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from arbora.api.invoke.pipeline import OperationRefused

IngestSyncMode = Literal["full", "incremental"]
IngestJobStatus = Literal["queued", "running", "completed", "failed"]
IngestSourceState = Literal["active", "paused", "error"]


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
        self, *, tenant: str, repo_id: str, idempotency_key: str
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


async def index_repository(context: Any, params: Mapping[str, Any], op: Any) -> Any:
    """Request a durable repository index through the composed ingestion runner."""
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    runner = _bound_runner(context)
    repo_id = params["repo_id"]
    receipt = await runner.index_repository(
        tenant=context.caller.tenant,
        repo_id=repo_id,
        idempotency_key=context.idempotency_key,
    )
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
