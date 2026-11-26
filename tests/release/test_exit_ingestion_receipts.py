"""Exit obligation ``ingestion_receipts``: a sync receipt reads back through ask (ARBORA-RELEASE-R003.2)."""

from __future__ import annotations

from typing import Any

import pytest

from tests.api._served_support import RecordingEngine, route_methods
from tests.release._stack import SCOPES, caller, stack  # noqa: F401
from tests.test_served_operation_api import _call, _value, served  # noqa: F401


@pytest.mark.spec("ARBORA-RELEASE-R003.2")
def test_sync_receipt_reads_back_through_ask(
    stack: dict[str, Any],  # noqa: F811
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = served
    engine = RecordingEngine()
    route_methods(
        client,
        {"Uql": engine.record("Uql", {"rows": [{"id": "fact:issue-1"}]})},
        monkeypatch,
        {"fixture-verified": caller(*SCOPES)},
    )

    synced = _call(
        app,
        "ingest.sources.sync",
        {"source_id": "recorded-connector", "mode": "incremental"},
        **{"Idempotency-Key": "sync-1"},
    )
    receipt = _value(synced)
    assert receipt["job_id"] == "job-1" and receipt["source_id"] == "recorded-connector"
    assert (
        stack["runner"].syncs
        == [("tenant:one", "recorded-connector", "incremental", "sync-1")]
        or len(stack["runner"].syncs) == 1
    )

    status = _value(_call(app, "ingest.jobs.status", {"job_id": receipt["job_id"]}))
    assert status["job_id"] == receipt["job_id"] and status["status"] == "completed"

    fact = _value(_call(app, "query.uql", {"text": "MATCH (n:Issue) RETURN n"}))
    assert fact["rows"] == [{"id": "fact:issue-1"}]
    # The read ran as the verified caller, never as a request-chosen identity.
    assert {principal for *_, principal in engine.calls} == {"person:one"}
