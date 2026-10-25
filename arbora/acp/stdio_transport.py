"""Hard stdio bounds around the pinned ACP SDK's existing JSON-RPC codec."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import acp

if TYPE_CHECKING:
    from .adapter import HostedACPAgent


class _BoundedReader(asyncio.StreamReader):
    def __init__(
        self, source: asyncio.StreamReader, frame_bytes: int, messages: int
    ) -> None:
        super().__init__(limit=frame_bytes)
        self.source = source
        self.frame_bytes = frame_bytes
        self.remaining = messages

    async def readuntil(self, separator: object = b"\n") -> bytes:
        if separator != b"\n":
            raise ValueError("ACP stdio requires newline framing")
        # SDK 0.11 treats LimitOverrunError as a reason to accumulate more
        # chunks, not a hard limit. readline converts overflow to ValueError.
        line = await self.source.readline()
        if line and (len(line) > self.frame_bytes or not line.endswith(b"\n")):
            raise ValueError("ACP stdio frame limit or incomplete frame")
        if line:
            self.remaining -= 1
            if self.remaining < 0:
                raise ValueError("ACP stdio message limit")
        return line


async def serve(agent: HostedACPAgent) -> None:
    """No RPC reimplementation, listeners, unlimited frames or retained replay."""
    reader, writer = await acp.stdio_streams(limit=65536)
    bounded = _BoundedReader(reader, 65536, 512)
    try:
        async with asyncio.timeout(3600):
            await acp.run_agent(
                agent,
                use_unstable_protocol=True,  # Pinned session/close only; others refuse.
                input_stream=writer,
                output_stream=bounded,
                receive_timeout=300,
            )
    finally:
        try:
            await agent.aclose()
        finally:
            writer.close()
