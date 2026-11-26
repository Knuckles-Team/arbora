"""One served stack for the exit-criteria tests (ARBORA-RELEASE-R003.2 to R003.8).

The real invoke pipeline answers over HTTP; the engine is a recorded stand-in
that the caller's verified principal reaches. ``ingest_runner`` is the one
recorded connector runner a sync goes through. Import the fixtures with
``from tests.release._stack import stack  # noqa: F401``.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from arbora.ingest.service import IngestJobRecord, IngestSyncReceipt
from tests.api._served_support import person
from tests.test_served_operation_api import served  # noqa: F401

SCOPES = ("ingest:write", "ingest:read", "query:unified", "query:nl")


class RecordedRunner:
    """A recorded connector sync: the receipt it answers is read back by job id."""

    def __init__(self) -> None:
        self.syncs: list[tuple[str, str, str, str]] = []
        self.jobs: dict[str, IngestJobRecord] = {}

    async def sync_source(
        self,
        *,
        tenant: str,
        source_id: str,
        mode: str,
        idempotency_key: str,
        caller: object = None,
    ) -> IngestSyncReceipt:
        self.syncs.append((tenant, source_id, mode, idempotency_key))
        job_id = f"job-{len(self.syncs)}"
        self.jobs[job_id] = IngestJobRecord(
            job_id=job_id, tenant=tenant, status="completed"
        )
        return IngestSyncReceipt(
            job_id=job_id,
            source_id=source_id,
            tenant=tenant,
            mode=mode,
            status="queued",
        )

    async def get_job_status(self, *, tenant: str, job_id: str) -> IngestJobRecord:
        return self.jobs[job_id]


def caller(*scopes: str) -> Any:
    one = person("one")
    return replace(one, roles=(*one.roles, *scopes))


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Bind the recorded runner into the composed invocation (request before ``served``)."""
    import tests.test_served_operation_api as harness

    held: dict[str, Any] = {"runner": RecordedRunner()}
    compose = harness.compose_served_invocation

    def composing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {**kwargs["bindings"], "ingest_runner": held["runner"]}
        held["invocation"] = compose(**kwargs)
        return held["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", composing)
    return held
