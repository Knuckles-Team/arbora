"""Pack capture paces its requests below the connector rate limit (ARBORA-FLEET-R041.12)."""

from __future__ import annotations

import asyncio
import time

import pytest

from arbora.fleet.onboarding import CAPTURE_REQUEST_INTERVAL_S, _PacedSession


class _Session:
    def __init__(self) -> None:
        self.calls: list[float] = []
        self.name = "fixture"

    async def get_prompt(self, name: str, arguments: dict[str, str]) -> str:
        self.calls.append(time.monotonic())
        return name


@pytest.mark.spec("ARBORA-FLEET-R041.12")
def test_capture_requests_stay_under_ten_per_second() -> None:
    assert CAPTURE_REQUEST_INTERVAL_S >= 0.1  # the SDK default limit is 10/s

    inner = _Session()
    paced = _PacedSession(inner, interval_s=0.02)

    async def run() -> list[str]:
        return [await paced.get_prompt(f"p{i}", {}) for i in range(5)]

    assert asyncio.run(run()) == [f"p{i}" for i in range(5)]
    gaps = [b - a for a, b in zip(inner.calls, inner.calls[1:], strict=False)]
    assert min(gaps) >= 0.018
    assert paced.name == "fixture"  # plain attributes pass through
