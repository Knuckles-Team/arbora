"""Released native Tuig, real PTY, production sink and HTTP with controlled owners."""
# spec: ARBORA-HOST-R009.8.2.2.1

import asyncio
import platform
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arbora.agent_host.log_access import HostLogAccess, HostLogBatch
from arbora.agent_host.log_runtime import HostLogRuntime
from arbora.agent_host.log_sink import WorkspaceLogSink
from tests.agent_host.native_terminal import NativeTerminal
from tests.agent_host.native_terminal_trace import (
    NativeTransportTrace,
    is_log_unavailable_response,
)
from tests.agent_host.test_log_access import ControlledOwners, serve
from tests.agent_host.test_log_replay import access_for
from tests.agent_host.test_log_sink import runtime_for

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() not in {"x86_64", "aarch64"},
    reason="Released Tuig wheels qualify Linux x86_64 and aarch64 only",
)


def track_reads(
    access: HostLogAccess, monkeypatch: pytest.MonkeyPatch, trace: NativeTransportTrace
) -> list[tuple[str, str | None]]:
    """Observe the real adapter; never replace its authority or response."""
    calls: list[tuple[str, str | None]] = []
    original_reference, original_read = access.reference, access.read

    async def reference(credential: str) -> str:
        calls.append(("bind", None))
        return await trace.observe("bind", lambda: original_reference(credential))

    async def read(credential: str, reference: str, **kwargs: Any) -> HostLogBatch:
        calls.append(("read", kwargs["cursor"]))
        return await trace.observe(
            "read", lambda: original_read(credential, reference, **kwargs)
        )

    monkeypatch.setattr(access, "reference", reference)
    monkeypatch.setattr(access, "read", read)
    return calls


def track_admission(
    runtime: HostLogRuntime,
    monkeypatch: pytest.MonkeyPatch,
    trace: NativeTransportTrace,
) -> None:
    original_session = runtime.sessions.resolve_session
    original_emit = runtime.sink.emit

    async def session(credential: str) -> Any:
        return await trace.observe("session", lambda: original_session(credential))

    async def emit(**kwargs: Any) -> None:
        await trace.observe("emit", lambda: original_emit(**kwargs))

    monkeypatch.setattr(runtime.sessions, "resolve_session", session)
    monkeypatch.setattr(runtime.sink, "emit", emit)


def assert_stopped(
    terminal: NativeTerminal, calls: list[tuple[str, str | None]]
) -> None:
    before = list(calls)
    deadline = time.monotonic() + 0.75
    terminal.wait_until(lambda: time.monotonic() >= deadline)
    assert calls == before, "Refusal must stop polling without renewed admission"
    assert sum(kind == "bind" for kind, _ in calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("read_delay", [0, 0.6])
@pytest.mark.usefixtures("unsynced_log_writes")
async def test_production_sink_over_http_into_mounted_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, read_delay: float
) -> None:
    owners = ControlledOwners()
    runtime = runtime_for(owners, tmp_path, True)
    trace = NativeTransportTrace()
    calls = track_reads(runtime.access, monkeypatch, trace)
    track_admission(runtime, monkeypatch, trace)
    original_read = runtime.sink.read

    async def delayed_read(**kwargs: Any) -> HostLogBatch:
        await asyncio.sleep(read_delay)
        return await original_read(**kwargs)

    monkeypatch.setattr(runtime.sink, "read", delayed_read)
    try:
        with serve(None, runtime) as server:
            response = server.get("health", headers={"Authorization": "Bearer a"})
            assert response.is_success, (
                f"Health status={response.status_code}; "
                f"log_unavailable={is_log_unavailable_response(response)}; "
                f"transport={trace.summary()}"
            )
            terminal = NativeTerminal(str(server.base_url), trace.summary)
            try:
                terminal.open_logs()
                terminal.wait_for("Authenticated host request received")
                owners.allowed = False
                text = terminal.wait_for("Authenticated host access is required")
                assert "Authenticated host request received" not in text
                assert_stopped(terminal, calls)
                assert "Authenticated host request received" not in terminal.shown()
                terminal.quit()
                assert not server.is_closed
            finally:
                terminal.close()
    finally:
        runtime.sink.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("write_delay", [0, 0.04])
@pytest.mark.usefixtures("unsynced_log_writes")
async def test_restarted_source_metadata_and_rotation_gap_in_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_delay: float
) -> None:
    owners = ControlledOwners()
    owners.now = time.time()
    expiry = int(owners.now * 1000) + 600_000
    owners.states = {
        key: replace(value, expires_at_ms=expiry)
        for key, value in owners.states.items()
    }
    owners.change["expires_at_ms"] = expiry - 1000
    sink = WorkspaceLogSink(str(tmp_path), "host-1", max_bytes=1024)
    await sink.emit(
        tenant="tenant-a", request_id="original-request", message="retained-message"
    )
    sink.close()
    sink = WorkspaceLogSink(str(tmp_path), "host-2", max_bytes=1024)
    original_emit = sink._emit

    def delayed_emit(*args: Any) -> None:
        time.sleep(write_delay)
        original_emit(*args)

    monkeypatch.setattr(sink, "_emit", delayed_emit)
    access = access_for(sink, owners)
    trace = NativeTransportTrace()
    calls = track_reads(access, monkeypatch, trace)
    try:
        with serve(access) as server:
            terminal = NativeTerminal(str(server.base_url), trace.summary)
            try:
                terminal.open_logs()
                text = terminal.wait_for("retained-message")
                assert "host=host-1 seq=1 request=original-request" in text
                # Preserve the real reader's cursor while storage ages, rather than
                # race the sink's separate busy-I/O refusal with live polling.
                with terminal.stopped():
                    async with asyncio.timeout(3):
                        while sink._pending:
                            await asyncio.sleep(0.01)
                    for index in range(30):
                        await sink.emit(
                            tenant="tenant-a", request_id=str(index), message="x" * 400
                        )
                text = terminal.wait_for("resume position is no longer retained")
                assert "retained-message" not in text
                assert calls[-1][0] == "read" and calls[-1][1] is not None
                assert_stopped(terminal, calls)
                assert "retained-message" not in terminal.shown()
                terminal.quit()
            finally:
                terminal.close()
    finally:
        sink.close()
