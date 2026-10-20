"""Bounded ephemeral duplex connection; never an approval or identity store."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from uuid import uuid4

from .coding_session import CodingSessionAdmission, CodingSessionAdmitted
from .session_policy import AcpRefusal, AcpRefusalReason

MAX_MESSAGE_BYTES = 65_536
MAX_PENDING_REQUESTS = 8
MAX_QUEUED_EVENTS = 32


class ClientChannel:
    """One live client socket and bounded RPC correlation, discarded on close."""

    def __init__(
        self,
        session_id: str,
        session: CodingSessionAdmitted,
        admission: CodingSessionAdmission,
        refresh: Callable[[], Awaitable[None]],
    ) -> None:
        self.session_id = session_id
        self.session = session
        self._admission = admission
        self._refresh = refresh
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(
            MAX_QUEUED_EVENTS
        )
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._sequence = 0
        self.connected = False
        self.closed = False
        self.run_task: asyncio.Task[Any] | None = None
        self.prompt_lock = asyncio.Lock()

    def emit(self, message: dict[str, Any]) -> None:
        if self.closed:
            raise AcpRefusal(AcpRefusalReason.CLIENT_DISCONNECTED)
        if len(json.dumps(message).encode()) > MAX_MESSAGE_BYTES:
            raise ValueError("ACP message limit")
        self._sequence += 1
        try:
            self._events.put_nowait({"sequence": self._sequence, "message": message})
        except asyncio.QueueFull:
            self.close()
            raise AcpRefusal(AcpRefusalReason.CLIENT_DISCONNECTED) from None

    async def check(self, method: str) -> None:
        await self._refresh()
        self.session = self._admission.check_capability(
            self.session,
            method,
            client_connected=self.connected and not self.closed,
            chat_adapter_available=True,
        )

    async def preview(self, method: str) -> None:
        await self._refresh()
        self.session = self._admission.check_preview(
            self.session,
            method,
            client_connected=self.connected and not self.closed,
            chat_adapter_available=True,
        )

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        await self.check(method)
        if len(self._pending) >= MAX_PENDING_REQUESTS:
            raise ValueError("ACP pending request limit")
        request_id = uuid4().hex
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        try:
            self.emit(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": {**params, "sessionId": self.session_id},
                }
            )
            async with asyncio.timeout(30):
                return await future
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    def reply(self, message: dict[str, Any]) -> None:
        if message.get("jsonrpc") != "2.0" or "method" in message:
            raise ValueError("Invalid ACP response")
        request_id = message.get("id")
        if not isinstance(request_id, str):
            raise ValueError("Invalid ACP response id")
        future = self._pending.get(request_id)
        if future is None or future.done():
            raise ValueError("Unknown ACP response id")
        if len(json.dumps(message).encode()) > MAX_MESSAGE_BYTES:
            raise ValueError("ACP response limit")
        if "error" in message:
            future.set_exception(ValueError("Client refused request"))
        elif isinstance(message.get("result"), dict):
            future.set_result(message["result"])
        else:
            raise ValueError("Invalid ACP response result")

    async def events(self) -> AsyncIterator[bytes]:
        if self.closed:
            raise ValueError("ACP connection closed")
        try:
            yield b": connected\n\n"
            while not self.closed:
                try:
                    event = await asyncio.wait_for(self._events.get(), timeout=10)
                except TimeoutError:
                    await self._refresh()
                    self._admission.check_session(self.session)
                    yield b": keepalive\n\n"
                    continue
                if event is None:
                    return
                yield f"id: {event['sequence']}\ndata: {json.dumps(event['message'])}\n\n".encode()
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.connected = False
        if self.run_task is not None:
            self.run_task.cancel()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AcpRefusal(AcpRefusalReason.CLIENT_DISCONNECTED))
        while not self._events.empty():
            self._events.get_nowait()
        self._events.put_nowait(None)
