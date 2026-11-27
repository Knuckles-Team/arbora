"""Terminal-mode HTTP in one supervised background thread (ARBORA-HOST-R009.2, R009.4.2).

In terminal mode the host serves HTTP while the terminal client runs in the foreground.
The HTTP server runs in one named daemon thread, ``arbora-host-http``, owned by the
process's :class:`~arbora.mcp_server.composition.CoServiceSupervisor`. Arbora starts no
second engine or multiplexer: the thread serves the application built over the admitted
runtime resources. A host instance starts once; a second start of the same instance, a
start with no admitted authority and a bind that fails each refuse with their own code.
The terminal exit stops only the thread this start created and leaves every service the
supervisor merely borrowed running.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from dataclasses import dataclass
from typing import Any

__all__ = [
    "HTTP_THREAD_NAME",
    "HostLaunchRefused",
    "TerminalHttpHost",
    "TerminalHttpHosts",
]

HTTP_THREAD_NAME = "arbora-host-http"
_POLL_SECONDS = 0.02


class HostLaunchRefused(RuntimeError):
    """A terminal-mode launch refusal: ``HOST_BIND_FAILED``, ``HOST_AUTHORITY_ABSENT``
    or ``HOST_DUPLICATE_INSTANCE``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class TerminalHttpHost:
    """Evidence of one started host: its thread, endpoint, readiness and authority."""

    host_instance: str
    thread_name: str
    thread_id: int
    endpoint: str
    ready: bool
    authority: str


class TerminalHttpHosts:
    """Start and stop the owned HTTP thread of each terminal-mode host instance."""

    def __init__(self, supervisor: Any) -> None:
        self._supervisor = supervisor
        self._owned: dict[str, str] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _service(host_instance: str) -> str:
        return f"{HTTP_THREAD_NAME}:{host_instance}"

    def start(
        self,
        *,
        host_instance: str,
        app: Any,
        session: Any,
        authority: object | None,
        bind_host: str,
        port: int,
        ready_timeout: float = 10.0,
    ) -> TerminalHttpHost:
        """Serve ``app`` in the named thread and return once it accepts connections."""
        if authority is None:
            raise HostLaunchRefused("HOST_AUTHORITY_ABSENT")
        service = self._service(host_instance)
        with self._lock:
            if host_instance in self._owned:
                raise HostLaunchRefused("HOST_DUPLICATE_INSTANCE")
            self._owned[host_instance] = service
        try:
            return self._start_owned(
                host_instance,
                service,
                app,
                session,
                authority,
                bind_host,
                port,
                ready_timeout,
            )
        except BaseException:
            with self._lock:
                self._owned.pop(host_instance, None)
            raise

    def _start_owned(
        self,
        host_instance: str,
        service: str,
        app: Any,
        session: Any,
        authority: object,
        bind_host: str,
        port: int,
        ready_timeout: float,
    ) -> TerminalHttpHost:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((bind_host, port))
        except OSError:
            listener.close()
            raise HostLaunchRefused("HOST_BIND_FAILED") from None
        endpoint = f"http://{bind_host}:{listener.getsockname()[1]}"
        started = threading.Event()

        def run(stop_event: threading.Event) -> None:
            import uvicorn

            server = uvicorn.Server(
                uvicorn.Config(app, lifespan="on", access_log=False, log_config=None)
            )

            async def serve() -> None:
                task = asyncio.ensure_future(server.serve(sockets=[listener]))
                try:
                    while not stop_event.is_set() and not task.done():
                        if server.started:
                            started.set()
                        await asyncio.sleep(_POLL_SECONDS)
                finally:
                    server.should_exit = True
                    await task

            try:
                asyncio.run(serve())
            finally:
                listener.close()

        try:
            self._supervisor.start_service(
                service, run, session, thread_name=HTTP_THREAD_NAME
            )
        except RuntimeError:
            listener.close()
            raise HostLaunchRefused("HOST_DUPLICATE_INSTANCE") from None
        thread = self._supervisor.service_thread(service)
        ready = started.wait(ready_timeout)
        if not ready:
            self._supervisor.stop_service(service)
            raise HostLaunchRefused("HOST_BIND_FAILED")
        return TerminalHttpHost(
            host_instance=host_instance,
            thread_name=HTTP_THREAD_NAME,
            thread_id=0 if thread is None or thread.ident is None else thread.ident,
            endpoint=endpoint,
            ready=ready,
            authority=f"authority:{id(authority):x}",
        )

    def stop(self, host_instance: str, timeout: float = 5.0) -> bool:
        """Stop the thread this start created; a service it did not start is untouched."""
        with self._lock:
            service = self._owned.get(host_instance)
        if service is None:
            return True
        stopped = self._supervisor.stop_service(service, timeout)
        if stopped:
            with self._lock:
                self._owned.pop(host_instance, None)
        return stopped
