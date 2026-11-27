"""The gateway's owned run-time state (ARBORA-HOST-R039.7).

``GatewayState`` is the ``gateway`` field of the host context. It owns its lock and
changes state only through its own methods. Routers are built per host by
``build_<name>_router(gateway)``; nothing here is a module-level object.
"""

from __future__ import annotations

import threading
from typing import Any

from arbora.gateway.ports import GatewayApplicationPort

__all__ = ["GatewayState", "gateway_of"]


class GatewayState:
    """Every owned run-time object of one gateway."""

    __slots__ = ("_application", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._application: GatewayApplicationPort | None = None

    def bind_application(self, application: GatewayApplicationPort) -> None:
        """Install the application adapter once at the composition root."""
        if not isinstance(application, GatewayApplicationPort):
            raise TypeError("application does not implement GatewayApplicationPort")
        with self._lock:
            self._application = application

    def application(self) -> GatewayApplicationPort:
        """Return the bound adapter, failing closed before composition."""
        with self._lock:
            application = self._application
        if application is None:
            raise RuntimeError(
                "arbora gateway application port is not configured; "
                "the composition root must bind it before serving requests"
            )
        return application


def gateway_of(carrier: Any) -> GatewayState:
    """The gateway field of the host the request's application was composed over.

    ``carrier`` is a request or websocket; the composition sets ``app.state.host``
    once when it mounts the routes.
    """
    return carrier.app.state.host.gateway
