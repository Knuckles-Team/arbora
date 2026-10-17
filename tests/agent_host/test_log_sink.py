"""Real owner-only sink and HTTP projection with controlled grant owners."""
# spec: GRAPHOS-HOST-R009.8

import asyncio
import json
import os
import time
from dataclasses import replace
from pathlib import Path

import pytest

from graph_os.agent_host.log_runtime import HostLogRuntime
from graph_os.agent_host.log_sink import WorkspaceLogSink
from tests.agent_host.test_log_access import ControlledOwners, read, reference, serve


def test_redaction_rotation_and_private_projection(tmp_path: Path) -> None:
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    secret = "Bearer " + "A" * 30

    async def exercise() -> None:
        for i in range(140):
            await sink.emit(
                tenant="tenant-a", request_id=str(i), message=f"event {i} {secret}"
            )
        await sink.emit(tenant="tenant-b", request_id="other", message="private-b")
        batch = await sink.read(
            tenant="tenant-a",
            workspace=str(tmp_path),
            host_instance="host-1",
            cursor=None,
            limit=128,
        )
        assert batch.gap and len(batch.records) == 128
        assert all(
            secret not in record.message and "private-b" not in record.message
            for record in batch.records
        )
        with pytest.raises(PermissionError):
            await sink.read(
                tenant="tenant-b",
                workspace=str(tmp_path),
                host_instance="host-1",
                cursor=batch.cursor,
                limit=128,
            )

    try:
        asyncio.run(exercise())
        files = list((tmp_path / "agent_data/logs").iterdir())
        assert len(files) == 5
        for path in files:
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.stat().st_size <= 1024
            assert secret not in path.read_text()
            for line in path.read_text().splitlines():
                assert json.loads(line)["host_instance"] == "host-1"
    finally:
        sink.close()
        sink.close()


@pytest.mark.parametrize("unsafe", ["directory-link", "file-link", "hard-link", "mode"])
def test_unsafe_sink_refuses(tmp_path: Path, unsafe: str) -> None:
    logs = tmp_path / "agent_data/logs"
    logs.mkdir(parents=True, mode=0o700)
    target = tmp_path / "private-target"
    target.write_text("untouched")
    target.chmod(0o600)
    if unsafe == "directory-link":
        logs.rmdir()
        logs.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "file-link":
        (logs / "server.log").symlink_to(target)
    elif unsafe == "hard-link":
        os.link(target, logs / "server.log")
    else:
        (logs / "server.log").write_text("unsafe")
        (logs / "server.log").chmod(0o644)
    with pytest.raises((PermissionError, OSError)):
        WorkspaceLogSink(str(tmp_path), "host-1")
    assert target.read_text() == "untouched"


def runtime_for(
    owners: ControlledOwners, tmp_path: Path, enabled: bool
) -> HostLogRuntime:
    expiry = int(time.time() * 1000) + 60_000
    owners.states = {
        key: replace(value, expires_at_ms=expiry)
        for key, value in owners.states.items()
    }
    owners.change["expires_at_ms"] = expiry - 1000
    return HostLogRuntime.compose(
        sessions=owners,
        grants=owners,
        workspace=str(tmp_path),
        interface_mode="terminal",
        web_logs=enabled,
    )


def test_real_http_request_reaches_production_sink_and_authorized_read(
    tmp_path: Path,
) -> None:
    owners = ControlledOwners()
    runtime = runtime_for(owners, tmp_path, True)
    try:
        with serve(None, runtime) as client:
            assert (
                client.get("/health", headers={"Authorization": "Bearer a"}).status_code
                == 200
            )
            ref = reference(client)
            response = read(client, ref)
            assert response.status_code == 200
            assert response.json()["records"] == [
                {"message": "Authenticated host request received"}
            ]
            assert read(client, reference(client, "b"), "b").json()["records"] == []
            owners.allowed = False
            assert (
                read(client, ref, cursor=response.json()["cursor"]).status_code == 403
            )
        lines = (tmp_path / "agent_data/logs/server.log").read_text().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["tenant"] == "tenant-a"
    finally:
        runtime.sink.close()


def test_disabled_viewing_keeps_required_sink(tmp_path: Path) -> None:
    owners = ControlledOwners()
    runtime = runtime_for(owners, tmp_path, False)
    try:
        with serve(None, runtime) as client:
            assert (
                client.get("/health", headers={"Authorization": "Bearer a"}).status_code
                == 200
            )
            assert (
                client.post(
                    "/host/logs/reference", headers={"Authorization": "Bearer a"}
                ).status_code
                == 403
            )
        assert (tmp_path / "agent_data/logs/server.log").stat().st_size > 0
    finally:
        runtime.sink.close()


def test_second_sink_and_fifo_refuse(tmp_path: Path) -> None:
    sink = WorkspaceLogSink(str(tmp_path), "host-one")
    try:
        with pytest.raises(OSError):
            WorkspaceLogSink(str(tmp_path), "host-two")
    finally:
        sink.close()
    path = tmp_path / "agent_data/logs/server.log"
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises((OSError, PermissionError)):
        WorkspaceLogSink(str(tmp_path), "host-three")


def test_cancelled_writer_retains_admission_until_worker_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    sink = WorkspaceLogSink(str(tmp_path), "host-1")
    entered, release = threading.Event(), threading.Event()
    original = sink._emit

    def blocked(*args):
        entered.set()
        assert release.wait(3)
        original(*args)

    monkeypatch.setattr(sink, "_emit", blocked)

    async def exercise() -> None:
        pending = asyncio.create_task(
            sink.emit(tenant="a", request_id="r", message="safe")
        )
        assert await asyncio.to_thread(entered.wait, 1)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        for _ in range(20):
            with pytest.raises(PermissionError):
                await sink.emit(tenant="a", request_id="r", message="no queue")
        for _ in range(20):
            with pytest.raises(PermissionError):
                await sink.read(
                    tenant="a",
                    workspace=str(tmp_path),
                    host_instance="host-1",
                    cursor=None,
                    limit=128,
                )
        assert len(sink._pending) == 1
        release.set()
        while sink._pending:
            await asyncio.sleep(0.01)

    try:
        asyncio.run(exercise())
        assert (
            len((tmp_path / "agent_data/logs/server.log").read_text().splitlines()) == 1
        )
    finally:
        release.set()
        sink.close()
