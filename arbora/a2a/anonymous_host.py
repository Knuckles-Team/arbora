"""The host's mount of the none-mode anonymous task owner (ARBORA-IDENTITY-R004.8.2).

In explicit none mode a ``POST /a2a`` is answered by the read-only anonymous application
when the engine reports ``GetRunWorkItem`` support. The owner composes on the first such
request over a native client in none mode; without support, or while the engine is
unreachable, the request goes to the verified application as before.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from arbora.core.settings import RuntimeConfig

logger = logging.getLogger(__name__)

__all__ = ["AnonymousA2AEntry", "anonymous_a2a_provider"]

Provider = Callable[[], Awaitable[Any]]


def anonymous_a2a_provider(
    *,
    origin: str,
    graph: str,
    revision: str,
    connect: Callable[[], Awaitable[Any]],
    config: RuntimeConfig,
) -> Provider:
    """A provider of the anonymous application: composed once, ``None`` until it can."""
    from .anonymous import AnonymousReadPolicy
    from .composition import compose_anonymous_a2a

    policy = AnonymousReadPolicy(origin, "local", revision, frozenset({"tasks/get"}))
    lock = asyncio.Lock()
    composed: list[Any] = []

    async def provide() -> Any:
        async with lock:
            if composed:
                return composed[0]
            try:
                app = await compose_anonymous_a2a(
                    await connect(), graph=graph, policy=policy, config=config
                )
            except Exception as exc:
                logging.getLogger(__name__).debug(
                    "no owner leaves the verified route", exc_info=True
                )
                logger.warning("anonymous a2a unavailable: %s", type(exc).__name__)
                return None
            if app is not None:
                composed.append(app)
            return app

    return provide


class AnonymousA2AEntry:
    """The ``/a2a`` entry: the anonymous application in none mode, else the verified one."""

    def __init__(self, verified: Any, provider: Provider | None, path: str) -> None:
        self._verified = verified
        self._provider = provider
        self._path = path
        self.__name__ = getattr(verified, "__name__", "a2a_agent")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        from arbora.security.anonymous import enabled

        if (
            self._provider is not None
            and enabled()
            and scope.get("method") == "POST"
            and scope.get("path") == self._path
        ):
            app = await self._provider()
            if app is not None:
                await app(scope, receive, send)
                return
        await self._verified(scope, receive, send)
