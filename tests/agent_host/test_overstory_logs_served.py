"""Served host-log contract the Overstory log view reads, over real HTTP and real retention files."""
# spec: ARBORA-HOST-R009.8.2.2.2

import asyncio
from pathlib import Path

import pytest

from arbora.agent_host.log_sink import WorkspaceLogSink
from tests.agent_host.test_log_access import ControlledOwners, reference, serve
from tests.agent_host.test_log_replay import access_for

RECORD_FIELDS = {
    "host_instance",
    "sequence",
    "request",
    "timestamp",
    "severity",
    "message",
}


def read_v2(client, ref: str, cursor: str | None = None):
    return client.post(
        "/host/logs/read",
        headers={"Authorization": "Bearer a"},
        json={"version": 2, "log_ref": ref, "cursor": cursor, "limit": 128},
    )


@pytest.mark.spec("ARBORA-HOST-R009.8.2.2.2")
def test_retention_gap_clears_protected_content(tmp_path: Path) -> None:
    owners = ControlledOwners()
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    asyncio.run(
        sink.emit(tenant="tenant-a", request_id="origin-request", message="held-text")
    )
    try:
        with serve(access_for(sink, owners)) as client:
            ref = reference(client)
            first = read_v2(client, ref)
            assert first.status_code == 200
            batch = first.json()
            assert batch["gap"] is False
            (record,) = batch["records"]
            assert set(record) == RECORD_FIELDS
            assert (record["host_instance"], record["request"]) == (
                "host-1",
                "origin-request",
            )
            assert record["sequence"] == 1 and record["message"] == "held-text"

            for index in range(30):
                asyncio.run(
                    sink.emit(
                        tenant="tenant-a", request_id=str(index), message="x" * 400
                    )
                )
            # A stale cursor answers one fixed refusal and carries no retained text,
            # metadata or cursor: the web view drops what it holds on this answer.
            stale = read_v2(client, ref, batch["cursor"])
            assert stale.status_code == 409
            assert stale.json() == {"detail": "retention_gap"}
            assert "held-text" not in stale.text

            # A fresh read past the rotation reports the gap, keeps every record's
            # original host metadata and never replays the rotated-out content.
            resumed = read_v2(client, reference(client))
            assert resumed.status_code == 200
            tail = resumed.json()
            assert tail["gap"] is True
            assert tail["records"] and tail["cursor"]
            assert all(set(item) == RECORD_FIELDS for item in tail["records"])
            assert all(item["host_instance"] == "host-1" for item in tail["records"])
            assert min(item["sequence"] for item in tail["records"]) > 1
            assert "held-text" not in resumed.text

            # Revocation clears the same way: one fixed refusal and no content.
            owners.allowed = False
            denied = read_v2(client, ref, tail["cursor"])
            assert denied.status_code == 403
            assert denied.json() == {"detail": "workspace_denied"}
    finally:
        sink.close()
