"""Shared fixtures for the messaging front end."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest

from arbora.messaging.host_ports import reset_host_ports


@pytest.fixture(autouse=True)
def _unbound_host_ports() -> Iterator[None]:
    reset_host_ports()
    yield
    reset_host_ports()


class FakeTimer:
    """A manual clock with a matching ``sleep`` for burst timers."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, asyncio.Future[None]]] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sleepers.append((self.now + delay, future))
        await future

    async def advance(self, delay: float) -> None:
        """Move the clock forward and run every timer that is now due."""
        await self._settle()
        self.now += delay
        due = [f for deadline, f in self._sleepers if deadline <= self.now]
        self._sleepers = [(d, f) for d, f in self._sleepers if d > self.now]
        for future in due:
            if not future.done():
                future.set_result(None)
        await self._settle()

    @staticmethod
    async def _settle() -> None:
        for _ in range(10):
            await asyncio.sleep(0)


@pytest.fixture
def timer() -> FakeTimer:
    return FakeTimer()
