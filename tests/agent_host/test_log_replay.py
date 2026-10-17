"""Real retained-file restart and HTTP admission; no deployed policy issuer."""
# spec: ARBORA-HOST-R009.8.2.1

import asyncio
import json
import os
from functools import partial
from pathlib import Path

import pytest

from arbora.agent_host.log_access import HostLogAccess, HostLogRetentionGap
from arbora.agent_host.log_sink import WorkspaceLogSink
from tests.agent_host.test_log_access import ControlledOwners, read, reference, serve


def access_for(sink: WorkspaceLogSink, owners: ControlledOwners) -> HostLogAccess:
    return HostLogAccess(
        sessions=owners,
        grants=owners,
        source=sink,
        workspace=sink.workspace,
        host_instance=sink.host_instance,
        interface_mode="terminal",
        web_logs=True,
        clock=lambda: owners.now,
    )


def test_http_restart_requires_fresh_grant_and_retains_original_metadata(
    tmp_path: Path,
) -> None:
    owners = ControlledOwners()
    sink = WorkspaceLogSink(str(tmp_path), "host-1")
    asyncio.run(sink.emit(tenant="tenant-a", request_id="request-one", message="one"))
    asyncio.run(sink.emit(tenant="tenant-a", request_id="request-two", message="two"))
    with serve(access_for(sink, owners)) as client:
        old_ref = reference(client)
        first = read(client, old_ref, limit=1).json()
        assert first["records"] == [{"message": "one"}]
    sink.close()
    restarted = WorkspaceLogSink(str(tmp_path), "host-2")
    try:
        with serve(access_for(restarted, owners)) as client:
            assert read(client, old_ref, cursor=first["cursor"]).status_code == 403
            fresh = reference(client)
            response = read(client, fresh, cursor=first["cursor"])
            assert response.status_code == 200
            assert response.json()["records"] == [{"message": "two"}]
            owners.change["source_generations"] = ("host-2",)
            assert (
                read(client, fresh, cursor=response.json()["cursor"]).status_code == 403
            )
            assert (
                read(client, reference(client), cursor=first["cursor"]).status_code
                == 403
            )
        batch = asyncio.run(
            restarted.read(
                tenant="tenant-a",
                workspace=str(tmp_path),
                host_instance="host-2",
                source_generations=("host-1",),
                cursor=first["cursor"],
                limit=128,
            )
        )
        record = batch.records[0]
        assert (
            record.host_instance,
            record.sequence,
            record.request,
            record.severity,
        ) == ("host-1", 2, "request-two", "INFO")
        assert record.timestamp > 0
    finally:
        restarted.close()


def test_rotated_anchor_returns_explicit_http_gap(tmp_path: Path) -> None:
    owners = ControlledOwners()
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    asyncio.run(sink.emit(tenant="tenant-a", request_id="first", message="first"))
    with serve(access_for(sink, owners)) as client:
        ref = reference(client)
        cursor = read(client, ref).json()["cursor"]
        for index in range(30):
            asyncio.run(
                sink.emit(tenant="tenant-a", request_id=str(index), message="x" * 400)
            )
        response = read(client, ref, cursor=cursor)
        assert response.status_code == 409
        assert response.json() == {"detail": "retention_gap"}
    sink.close()
    restarted = WorkspaceLogSink(str(tmp_path), "host-2", max_bytes=1024)
    try:
        with pytest.raises(HostLogRetentionGap):
            asyncio.run(
                restarted.read(
                    tenant="tenant-a",
                    workspace=str(tmp_path),
                    host_instance="host-2",
                    source_generations=("host-1",),
                    cursor=cursor,
                    limit=128,
                )
            )
    finally:
        restarted.close()


@pytest.mark.parametrize("mutation", ["signature", "tenant", "generations"])
def test_durable_cursor_rejects_tampering_and_scope_changes(
    tmp_path: Path, mutation: str
) -> None:
    sink = WorkspaceLogSink(str(tmp_path), "host-1")

    async def exercise() -> None:
        await sink.emit(tenant="tenant-a", request_id="one", message="safe")
        source_read = partial(
            sink.read,
            workspace=str(tmp_path),
            host_instance="host-1",
            limit=128,
        )
        tenant = "tenant-a"
        generations: tuple[str, ...] = ("host-1",)
        batch = await source_read(
            tenant=tenant, source_generations=generations, cursor=None
        )
        cursor = batch.cursor
        assert cursor is not None
        if mutation == "signature":
            cursor = "A" + cursor[1:]
        elif mutation == "tenant":
            tenant = "tenant-b"
        else:
            generations = ("host-1", "host-2")
        with pytest.raises(PermissionError):
            await source_read(
                tenant=tenant, source_generations=generations, cursor=cursor
            )

    try:
        asyncio.run(exercise())
    finally:
        sink.close()


@pytest.mark.parametrize(
    "unsafe", ["symlink", "malformed", "key-mode", "generation-reuse"]
)
def test_restart_refuses_unsafe_history(tmp_path: Path, unsafe: str) -> None:
    sink = WorkspaceLogSink(str(tmp_path), "host-1")
    asyncio.run(sink.emit(tenant="tenant-a", request_id="one", message="safe"))
    sink.close()
    logs = tmp_path / "agent_data/logs"
    if unsafe == "symlink":
        (logs / "server.log").rename(logs / "saved")
        (logs / "server.log").symlink_to(logs / "saved")
    elif unsafe == "malformed":
        (logs / "server.log").write_bytes(b'{"partial":')
    elif unsafe == "key-mode":
        (logs / ".cursor-key").chmod(0o644)
    with pytest.raises((PermissionError, OSError)):
        WorkspaceLogSink(
            str(tmp_path), "host-1" if unsafe == "generation-reuse" else "host-2"
        )


def test_revocation_during_source_gap_suppresses_retention_metadata(
    tmp_path: Path,
) -> None:
    owners = ControlledOwners()
    access = HostLogAccess(
        sessions=owners,
        grants=owners,
        source=owners,
        workspace=str(tmp_path),
        host_instance="host-1",
        interface_mode="terminal",
        web_logs=True,
        clock=lambda: owners.now,
    )

    def revoke_and_gap() -> None:
        owners.allowed = False
        raise HostLogRetentionGap("retention_gap")

    owners.on_read = revoke_and_gap
    with serve(access) as client:
        response = read(client, reference(client), cursor="source-position")
        assert response.status_code == 403
        assert "retention_gap" not in response.text
        assert owners.calls == 1


@pytest.mark.parametrize("sequence", [1, 3])
def test_restart_refuses_duplicate_or_missing_middle_sequence(
    tmp_path: Path, sequence: int
) -> None:
    sink = WorkspaceLogSink(str(tmp_path), "host-1")
    asyncio.run(sink.emit(tenant="tenant-a", request_id="one", message="one"))
    asyncio.run(sink.emit(tenant="tenant-a", request_id="two", message="two"))
    sink.close()
    path = tmp_path / "agent_data/logs/server.log"
    lines = path.read_text().splitlines()
    second = json.loads(lines[1])
    second["sequence"] = sequence
    path.write_text(lines[0] + "\n" + json.dumps(second) + "\n")
    with pytest.raises(PermissionError):
        WorkspaceLogSink(str(tmp_path), "host-2")


def test_ambiguous_sync_failure_poisoned_until_reopen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sink = WorkspaceLogSink(str(tmp_path), "host-1")

    def failed_sync(fd: int) -> None:
        raise OSError("sync failed")

    monkeypatch.setattr(os, "fsync", failed_sync)
    try:
        with pytest.raises(OSError):
            asyncio.run(sink.emit(tenant="tenant-a", request_id="one", message="one"))
        with pytest.raises(PermissionError):
            asyncio.run(sink.emit(tenant="tenant-a", request_id="two", message="two"))
        assert (
            len((tmp_path / "agent_data/logs/server.log").read_text().splitlines()) == 1
        )
    finally:
        sink.close()
