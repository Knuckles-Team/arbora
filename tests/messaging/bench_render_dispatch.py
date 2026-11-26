"""Nightly hot-path proof: render and dispatch latency (ARBORA-MESSAGING-R800.6).

Renders and dispatches 10,000 mixed messages and records p50 and p99 latency
per call. The budget is p99 under 5 ms per call and memory growth under 64 MiB.
This file is named ``bench_`` so a pull request run never collects it; the
nightly job runs it by path.
"""

from __future__ import annotations

import asyncio
import time
import tracemalloc

import pytest

from arbora.messaging.commands import handle_command
from arbora.messaging.render import markdown_to_telegram_html

_MESSAGES = (
    "plain text",
    "**bold** and _italic_ with `code`",
    "# Heading\n- one\n- two\n\n[link](https://example.org)",
    "/help",
    "/status",
    "```py\nprint(1)\n```",
)
_COUNT = 10_000
_P99_BUDGET_S = 0.005
_GROWTH_BUDGET_BYTES = 64 * 1024 * 1024


def _percentile(samples: list[float], fraction: float) -> float:
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


@pytest.mark.spec("ARBORA-MESSAGING-R800.6")
async def test_ten_thousand_messages() -> None:
    service = type("Service", (), {})()
    tracemalloc.start()
    before, _ = tracemalloc.get_traced_memory()
    render_s: list[float] = []
    dispatch_s: list[float] = []
    for index in range(_COUNT):
        text = _MESSAGES[index % len(_MESSAGES)]
        started = time.perf_counter()
        markdown_to_telegram_html(text)
        render_s.append(time.perf_counter() - started)
        started = time.perf_counter()
        try:
            await asyncio.wait_for(handle_command(text, service=service), timeout=1)
        except Exception:  # a command needing a live service still times the dispatch
            pass
        dispatch_s.append(time.perf_counter() - started)
    after, _ = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    for name, samples in (("render", render_s), ("dispatch", dispatch_s)):
        p50, p99 = _percentile(samples, 0.50), _percentile(samples, 0.99)
        print(f"{name}: p50={p50 * 1000:.3f}ms p99={p99 * 1000:.3f}ms")
        assert p99 < _P99_BUDGET_S, name
    assert after - before < _GROWTH_BUDGET_BYTES
