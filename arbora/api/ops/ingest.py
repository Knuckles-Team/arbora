"""Ingest operations for the Arbora intent registry (ARBORA-OPS-R020.1, R020.2).

ARBORA-OPS-R020 exposes ingest operations for repository indexing, source
sync, pack and job management, drift listing and repair, and embedding
admission, calling the ingestion SDK only through a typed runner facade
(:mod:`arbora.ingest.service`). R020.1 proved the port's first entry point,
``ingest.sources.sync``: a durable job receipt on success and a typed
``UNAVAILABLE`` refusal when no runner is composed. This slice (R020.2) adds
``ingest.repositories.index`` and ``ingest.sources.{list,status}`` through
the same port. The remaining operation family (``ingest.packs.*``,
``ingest.jobs.*``, ``ingest.drift.*``, ``ingest.embedding.*``) is tracked as
ARBORA-OPS-R020.3 through R020.5 in ``specs/hosted-api-operations/``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.registry import (
    AuditClass,
    Composite,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)
from arbora.ingest.service import (
    cancel_job,
    get_job_status,
    get_source_status,
    index_repository,
    list_jobs,
    list_packs,
    list_sources,
    prioritize_job,
    submit_ingest,
    sync_source,
)

#: Re-exported so ``registry_factory.get_registry()`` can resolve each
#: ``Composite(handler="arbora.api.ops.ingest.<name>")`` binding from this
#: module's own namespace, matching the ``access`` ops-module convention even
#: though the implementation lives in the service layer
#: (``arbora.ingest.service``).
__all__ = [
    "NATIVE_IDEMPOTENCY",
    "operations",
    "cancel_job",
    "get_job_status",
    "get_source_status",
    "index_repository",
    "list_jobs",
    "list_packs",
    "list_sources",
    "prioritize_job",
    "submit_ingest",
    "sync_source",
]


#: Operations whose idempotency the engine WorkItem authority owns
#: (ARBORA-OPS-R020.6.2): the sync's WorkItem is keyed by the caller's
#: idempotency key, so a replay returns the same durable job.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "ingest.sources.sync": "arbora.ingest.connector_sources:ConnectorIngestRunner",
}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestSourceSyncParams(_Params):
    source_id: str = Field(min_length=1, max_length=256)
    mode: str = Field(default="incremental", pattern=r"^(full|incremental)$")


class IngestSyncResult(_Params):
    value: dict[str, str]


class IngestRepositoryIndexParams(_Params):
    repo_id: str = Field(min_length=1, max_length=256)


class IngestIndexResult(_Params):
    value: dict[str, str]


class IngestSourcesListParams(_Params):
    pass


class IngestSourceListResult(_Params):
    value: dict[str, object]


class IngestSourceStatusParams(_Params):
    source_id: str = Field(min_length=1, max_length=256)


class IngestSourceStatusResult(_Params):
    value: dict[str, object]


class IngestPacksListParams(_Params):
    pass


class IngestPackListResult(_Params):
    value: dict[str, object]


class IngestJobStatusParams(_Params):
    job_id: str = Field(min_length=1, max_length=256)


class IngestJobStatusResult(_Params):
    value: dict[str, str]


class IngestSubmitParams(_Params):
    kind: Literal[
        "ingest", "ingest_url", "ingest_knowledge_pack", "fact_extract", "distill"
    ]
    target: str = Field(min_length=1, max_length=2048)
    options: dict[str, str] = Field(default_factory=dict, max_length=8)


class IngestSubmitResult(_Params):
    value: dict[str, str]


class IngestJobsListParams(_Params):
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=1, le=100)


class IngestJobsListResult(_Params):
    value: dict[str, object]


class IngestJobCancelParams(_Params):
    job_id: str = Field(min_length=1, max_length=256)


class IngestJobPrioritizeParams(_Params):
    job_id: str = Field(min_length=1, max_length=256)
    bucket: int = Field(ge=0, le=9)


class IngestJobControlResult(_Params):
    value: dict[str, object]


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="ingest.sources.sync",
            verb=Verb.ACT,
            summary="Request a durable sync of one ingestion source",
            examples=("sync this source now",),
            params=IngestSourceSyncParams,
            result=IngestSyncResult,
            binding=Composite(handler="arbora.api.ops.ingest.sync_source"),
            scopes=frozenset({"ingest:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="ingest.repositories.index",
            verb=Verb.ACT,
            summary="Request a durable index of one repository",
            examples=("index this repository now",),
            params=IngestRepositoryIndexParams,
            result=IngestIndexResult,
            binding=Composite(handler="arbora.api.ops.ingest.index_repository"),
            scopes=frozenset({"ingest:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="ingest.sources.list",
            verb=Verb.FIND,
            summary="List this tenant's ingestion sources",
            examples=("list my ingestion sources",),
            params=IngestSourcesListParams,
            result=IngestSourceListResult,
            binding=Composite(handler="arbora.api.ops.ingest.list_sources"),
            scopes=frozenset({"ingest:read"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="ingest.sources.status",
            verb=Verb.ASK,
            summary="Show one ingestion source's current status",
            examples=("show the status of this ingestion source",),
            params=IngestSourceStatusParams,
            result=IngestSourceStatusResult,
            binding=Composite(handler="arbora.api.ops.ingest.get_source_status"),
            scopes=frozenset({"ingest:read"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="ingest.packs.list",
            verb=Verb.FIND,
            summary="List this tenant's ingestion packs",
            examples=("list my ingestion packs",),
            params=IngestPacksListParams,
            result=IngestPackListResult,
            binding=Composite(handler="arbora.api.ops.ingest.list_packs"),
            scopes=frozenset({"ingest:read"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="ingest.jobs.status",
            verb=Verb.ASK,
            summary="Show one durable ingestion job's current status",
            examples=("show the status of this ingestion job",),
            params=IngestJobStatusParams,
            result=IngestJobStatusResult,
            binding=Composite(handler="arbora.api.ops.ingest.get_job_status"),
            scopes=frozenset({"ingest:read"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="ingest.submissions.create",
            verb=Verb.ACT,
            summary="Queue a durable ingest, URL, pack, fact-extract or distill job",
            examples=("ingest this url", "distill this document"),
            params=IngestSubmitParams,
            result=IngestSubmitResult,
            binding=Composite(handler="arbora.api.ops.ingest.submit_ingest"),
            scopes=frozenset({"ingest:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="ingest.jobs.list",
            verb=Verb.FIND,
            summary="List this tenant's durable ingestion jobs",
            examples=("list my ingestion jobs",),
            params=IngestJobsListParams,
            result=IngestJobsListResult,
            binding=Composite(handler="arbora.api.ops.ingest.list_jobs"),
            scopes=frozenset({"ingest:read"}),
            effect=Effect.READ,
        ),
        OpSpec(
            id="ingest.jobs.cancel",
            verb=Verb.ACT,
            summary="Cancel one pending ingestion job",
            examples=("cancel this ingestion job",),
            params=IngestJobCancelParams,
            result=IngestJobControlResult,
            binding=Composite(handler="arbora.api.ops.ingest.cancel_job"),
            scopes=frozenset({"ingest:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.NATURAL,
            audit=AuditClass.EVENT,
        ),
        OpSpec(
            id="ingest.jobs.prioritize",
            verb=Verb.ACT,
            summary="Set the priority bucket of one ingestion job",
            examples=("prioritize this ingestion job",),
            params=IngestJobPrioritizeParams,
            result=IngestJobControlResult,
            binding=Composite(handler="arbora.api.ops.ingest.prioritize_job"),
            scopes=frozenset({"ingest:write"}),
            effect=Effect.WRITE,
            idempotency=Idempotency.NATURAL,
            audit=AuditClass.EVENT,
        ),
    )
