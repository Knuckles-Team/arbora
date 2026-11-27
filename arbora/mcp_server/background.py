"""The host's background services: started with the process, stopped with it.

Each service is an object with ``async run(should_stop)``. The host runs them on
one daemon thread with its own event loop, so a slow service never holds the
serving loop. :class:`SystemClock` is the only wall-clock source; services take
a :class:`~arbora.knowledge.curator_scheduler.Clock`, so tests inject their own.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from arbora.observability.state import Errors
from arbora.security.error_surface import bound_errors

logger = logging.getLogger(__name__)


class BackgroundService(Protocol):
    async def run(self, should_stop: Callable[[], bool]) -> None: ...


class SystemClock:
    """Monotonic time and real waiting, for the running host only."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class BackgroundHost:
    """Run every service on one daemon thread until :meth:`stop`."""

    def __init__(self, services: Sequence[BackgroundService], errors: Errors) -> None:
        self._services = tuple(services)
        self._errors = errors
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        if self._thread is not None or not self._services:
            return
        self._thread = threading.Thread(
            target=self._main, name="arbora-background", daemon=True
        )
        self._thread.start()
        self._ready.wait()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._cancel_all, loop)
        if self._thread is not None:
            self._thread.join(timeout)

    @staticmethod
    def _cancel_all(loop: asyncio.AbstractEventLoop) -> None:
        for task in asyncio.all_tasks(loop):
            task.cancel()

    def _main(self) -> None:
        with bound_errors(self._errors):
            self._serve_loop()

    def _serve_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        self._ready.set()
        try:
            loop.run_until_complete(self._run_all())
        except asyncio.CancelledError:
            pass
        finally:
            loop.close()

    async def _run_all(self) -> None:
        async def guarded(service: Any) -> None:
            try:
                await service.run(self._stop.is_set)
            except asyncio.CancelledError:
                raise
            except Exception:  # one service must not stop the rest
                logger.exception("a background service stopped")

        await asyncio.gather(*(guarded(s) for s in self._services))
