"""Lifespan gate for an owner-attested borrowed web application."""

from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from arbora.agent_host.start_admission import AdmittedHostStart, HostStartContext
from arbora.agent_host.start_configuration import (
    HostRuntimeResources,
    HostStartConfiguration,
)


class ManagedWebApplication:
    """Wrap without mutating the supplied application's routes or lifecycle.

    The construction owner qualifies request authority and resource ownership.
    This wrapper only gates startup; it does not authorize individual requests.
    Uvicorn must require lifespan support rather than use automatic detection.
    """

    def __init__(
        self,
        context: HostStartContext,
        admitted: AdmittedHostStart,
        configuration: HostStartConfiguration,
        runtime: HostRuntimeResources,
    ) -> None:
        if not callable(runtime.web_application):
            raise ValueError("web_application_required")
        self._app = cast(ASGIApp, runtime.web_application)
        self._context = context
        self._admitted = admitted
        self._configuration = configuration
        self._runtime = runtime

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "lifespan":
            await self._app(scope, receive, send)
            return
        startup = await receive()
        if startup["type"] != "lifespan.startup":
            raise RuntimeError("lifespan startup required")
        try:
            await self._context.revalidate_runtime(
                self._admitted, self._configuration, self._runtime
            )
        except (PermissionError, TimeoutError, ValueError):
            await send(
                {"type": "lifespan.startup.failed", "message": "host admission refused"}
            )
            return

        pending: Message | None = startup

        async def replay_startup() -> Message:
            nonlocal pending
            if pending is not None:
                message, pending = pending, None
                return message
            return await receive()

        await self._app(scope, replay_startup, send)
