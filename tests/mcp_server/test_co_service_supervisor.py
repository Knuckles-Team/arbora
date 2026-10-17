"""Focused tests for the GraphOS-owned co-service supervisor."""

from __future__ import annotations

import contextvars
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.request import urlopen

import pytest

from graph_os.mcp_server.composition import CoServiceSupervisor


def test_native_supervisor_preserves_ambient_context() -> None:
    authority: contextvars.ContextVar[str] = contextvars.ContextVar(
        "test_co_service_authority"
    )
    authority.set("verified")
    observed: list[str] = []
    started = threading.Event()

    def run(stop_event: threading.Event) -> None:
        observed.append(authority.get())
        started.set()
        stop_event.wait()

    supervisor = CoServiceSupervisor()
    supervisor.start_service("test", run, session=object())

    assert started.wait(timeout=1.0)
    assert supervisor.running() == ("test",)
    assert supervisor.stop_all(timeout=1.0) is True
    assert observed == ["verified"]


def test_native_supervisor_rejects_missing_session() -> None:
    supervisor = CoServiceSupervisor()

    with pytest.raises(PermissionError, match="verified co-service session"):
        supervisor.start_service("test", lambda _stop: None, session=None)


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready")

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.mark.spec("GRAPHOS-HOST-R009.4.1")
def test_service_can_request_stop_without_joining_itself() -> None:
    supervisor = CoServiceSupervisor()
    requested = threading.Event()
    observed: list[bool] = []

    def run(stop: threading.Event) -> None:
        observed.append(supervisor.stop_service("self", timeout=1))
        assert stop.is_set()
        requested.set()

    supervisor.start_service("self", run, session=object())
    try:
        assert requested.wait(timeout=1)
        assert observed == [False]
    finally:
        assert supervisor.stop_service("self", timeout=1)


@pytest.mark.spec("GRAPHOS-HOST-R009.4.1")
def test_targeted_shutdown_preserves_borrowed_http_service() -> None:
    supervisor = CoServiceSupervisor()
    servers = [HTTPServer(("127.0.0.1", 0), _HealthHandler) for _ in range(2)]

    def serve(server: HTTPServer, stop: threading.Event) -> None:
        server.timeout = 0.02
        while not stop.is_set():
            server.handle_request()

    try:
        for name, server in zip(("owned", "borrowed"), servers, strict=True):
            supervisor.start_service(
                name, lambda stop, server=server: serve(server, stop), session=object()
            )
        for server in servers:
            with urlopen(
                f"http://127.0.0.1:{server.server_port}/health", timeout=1
            ) as response:
                assert response.read() == b"ready"
        assert supervisor.stop_service("owned", timeout=1)
        assert supervisor.running() == ("borrowed",)
        with urlopen(
            f"http://127.0.0.1:{servers[1].server_port}/health", timeout=1
        ) as response:
            assert response.read() == b"ready"
        assert supervisor.stop_service("owned", timeout=0)
    finally:
        assert supervisor.stop_all(timeout=1)
        for server in servers:
            server.server_close()


@pytest.mark.spec("GRAPHOS-HOST-R009.4.1")
def test_shutdown_deadline_retains_stalled_handles_for_retry() -> None:
    supervisor = CoServiceSupervisor()
    release = threading.Event()
    started = threading.Barrier(4)

    def stalled(stop: threading.Event) -> None:
        started.wait(timeout=2)
        release.wait(timeout=5)
        stop.set()

    try:
        for index in range(3):
            supervisor.start_service(str(index), stalled, session=object())
        started.wait(timeout=2)
        before = time.monotonic()
        assert not supervisor.stop_all(timeout=0.1)
        assert time.monotonic() - before < 0.25
        assert len(supervisor.running()) == 3
        assert not supervisor.stop_service("0", timeout=0)
    finally:
        release.set()
        assert supervisor.stop_all(timeout=1)
