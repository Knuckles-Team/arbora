"""The host's owned browser-control state (ARBORA-HOST-R039.6).

``BrowserState`` is the ``browser`` field of the host context. It owns the one
browser-control service the WebUI registers, the event loop that service runs on, the
lock over both, and the capacity of the canonical browser request dispatch. It changes
state only through its own methods and is reached by argument.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

__all__ = ["BrowserState"]

#: How many canonical browser requests may be in flight at once.
DISPATCH_CAPACITY = 16


class BrowserState:
    """The service, owner loop and dispatch capacity of one host's browser control."""

    __slots__ = ("_lock", "_owner_loop", "_service", "capacity")

    def __init__(self, capacity: int = DISPATCH_CAPACITY) -> None:
        self._lock = threading.RLock()
        self._service: Any | None = None
        self._owner_loop: asyncio.AbstractEventLoop | None = None
        #: Bounded slots a browser request holds from admission to completion.
        self.capacity = threading.BoundedSemaphore(capacity)

    def register(self, service: Any) -> None:
        """Register the exact service injected into overstory."""
        with self._lock:
            if self._service is service:
                return
            if self._service is not None:
                raise RuntimeError(
                    "a different live browser-control service is registered"
                )
            self._service = service
            self._owner_loop = None

    def unregister(self, service: Any) -> None:
        """Release only the exact service whose owning application is stopping."""
        with self._lock:
            if self._service is not service:
                raise RuntimeError(
                    "browser-control service unregister identity changed"
                )
            self._service = None
            self._owner_loop = None

    def bind_owner(self, service: Any) -> None:
        """Bind the registered service to overstory's current event loop."""
        loop = asyncio.get_running_loop()
        with self._lock:
            if self._service is None:
                return
            if self._service is not service:
                raise RuntimeError("browser-control owner does not match registration")
            if (
                self._owner_loop is not None
                and self._owner_loop is not loop
                and not self._owner_loop.is_closed()
            ):
                raise RuntimeError(
                    "browser-control service already has a live owner loop"
                )
            self._owner_loop = loop

    def authority(self) -> tuple[Any, asyncio.AbstractEventLoop]:
        """The registered service and its live owner loop, or refuse."""
        with self._lock:
            service = self._service
            loop = self._owner_loop
        if service is None or loop is None or loop.is_closed() or not loop.is_running():
            raise RuntimeError(
                "browser-control service is not attached to a live WebUI"
            )
        return service, loop

    def attached(self) -> bool:
        """True when a service is registered on a live, running owner loop."""
        with self._lock:
            loop = self._owner_loop
            return (
                self._service is not None
                and loop is not None
                and not loop.is_closed()
                and loop.is_running()
            )
