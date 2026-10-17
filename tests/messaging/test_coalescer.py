"""Burst coalescing: one flush per quiet window, a hard limit, and per-key bursts."""

from __future__ import annotations

from typing import Any

import pytest

from graph_os.messaging.coalescer import BurstCoalescer

pytestmark = pytest.mark.asyncio


def _coalescer(timer: Any, flushed: list[tuple[str, list[Any]]], **kw: float):
    async def on_flush(key: str, items: list[Any]) -> None:
        flushed.append((key, items))

    return BurstCoalescer(
        on_flush,
        window_s=kw.get("window_s", 1.0),
        max_wait_s=kw.get("max_wait_s", 10.0),
        clock=timer.clock,
        sleep=timer.sleep,
    )


@pytest.mark.parametrize(
    ("gaps", "expected"),
    [
        # Messages faster than the window collapse into one batch.
        ([0.2, 0.2, 0.2], [["m0", "m1", "m2", "m3"]]),
        # A pause longer than the window starts a second batch.
        ([0.2, 1.5, 0.2], [["m0", "m1"], ["m2", "m3"]]),
    ],
)
async def test_messages_inside_one_window_flush_once(timer, gaps, expected) -> None:
    flushed: list[tuple[str, list[Any]]] = []
    coalescer = _coalescer(timer, flushed)
    await coalescer.submit("chat", "m0")
    for index, gap in enumerate(gaps, start=1):
        await timer.advance(gap)
        await coalescer.submit("chat", f"m{index}")
    await timer.advance(1.0)
    assert [items for _key, items in flushed] == expected


async def test_hard_limit_flushes_a_user_who_never_pauses(timer) -> None:
    flushed: list[tuple[str, list[Any]]] = []
    coalescer = _coalescer(timer, flushed, window_s=1.0, max_wait_s=2.0)
    for index in range(6):
        await coalescer.submit("chat", index)
        await timer.advance(0.5)
    assert flushed[0] == ("chat", [0, 1, 2, 3, 4])


async def test_keys_flush_independently(timer) -> None:
    flushed: list[tuple[str, list[Any]]] = []
    coalescer = _coalescer(timer, flushed)
    await coalescer.submit("a", 1)
    await coalescer.submit("b", 2)
    await timer.advance(1.0)
    assert sorted(flushed) == [("a", [1]), ("b", [2])]


async def test_flush_handler_survives_its_own_awaits(timer) -> None:
    """The timer task that flushes must not cancel the handler it runs."""
    done: list[str] = []

    async def on_flush(_key: str, items: list[Any]) -> None:
        await timer.sleep(5.0)
        done.extend(items)

    coalescer = BurstCoalescer(
        on_flush, window_s=1.0, clock=timer.clock, sleep=timer.sleep
    )
    await coalescer.submit("chat", "reply")
    await timer.advance(1.0)
    await timer.advance(5.0)
    assert done == ["reply"]


async def test_zero_window_flushes_each_item(timer) -> None:
    flushed: list[tuple[str, list[Any]]] = []
    coalescer = _coalescer(timer, flushed, window_s=0.0, max_wait_s=0.0)
    await coalescer.submit("chat", 1)
    await coalescer.submit("chat", 2)
    await timer.advance(0.0)
    assert [items for _key, items in flushed] == [[1], [2]]
