"""The gateway health producer writes an anomaly's trend window as evidence.

A window that the shared kernel flags as anomalous is written as one
``MetricWindow`` evidence locus. A normal window, or a window before the
baseline has enough history, writes nothing. The store is a fake.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from graph_os.observability import gateway_health as gh
from graph_os.observability import health as h


class _FakeStore:
    def __init__(self) -> None:
        self.window_calls: list[tuple[bytes, dict]] = []

    def store_metric_window_evidence(self, data: bytes, **kwargs):
        self.window_calls.append((data, kwargs))
        return object()


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(gh, "_buffer", None)
    monkeypatch.setattr(gh, "_history", [])
    fake = _FakeStore()
    monkeypatch.setattr(gh, "_media_store", lambda: fake)
    return fake


def _trend(avg: float, index: int) -> dict:
    t0 = 1_000_000.0 + index * gh._WINDOW_S
    return {
        "min": avg - 0.01,
        "max": avg + 0.01,
        "avg": avg,
        "avg_control": None,
        "samples": 10,
        "window_s": gh._WINDOW_S,
        "start_at": t0,
        "end_at": t0 + gh._WINDOW_S,
    }


@pytest.mark.parametrize(
    ("history", "avg", "writes"),
    [(6, 5.0, 1), (6, 0.05, 0), (2, 5.0, 0)],
    ids=["anomalous-window", "normal-window", "short-history"],
)
@pytest.mark.asyncio
async def test_only_an_anomalous_window_is_written_as_evidence(
    monkeypatch, store, history, avg, writes
):
    monkeypatch.setattr(gh, "_history", [_trend(0.05, i) for i in range(history)])
    trend = _trend(avg, history)

    await gh._check_and_record(trend)

    assert len(store.window_calls) == writes
    assert gh._history[-1] == trend
    if writes:
        data, kwargs = store.window_calls[0]
        assert data == json.dumps(trend, sort_keys=True).encode("utf-8")
        assert kwargs["start_ms"] == int(trend["start_at"] * 1000)
        assert kwargs["end_ms"] == int(trend["end_at"] * 1000)


def test_record_request_duration_flush_triggers_the_check(monkeypatch):
    """The REAL entry point: enough `at=` samples to cross the window boundary
    schedules the background anomaly-check — proving the buffer -> flush ->
    check wiring, not just `_check_and_record` in isolation.
    """
    scheduled: list[dict] = []

    async def _fake_check(trend):
        scheduled.append(trend)

    monkeypatch.setattr(gh, "_check_and_record", _fake_check)

    import time as _time

    async def _drive():
        # First call lazily creates + caches the module-level buffer, seeded
        # from the real wall clock (`record_request_duration` always samples
        # `time.time()` — no injected clock, matching the real request path).
        gh.record_request_duration(0.05)
        buf = gh._buffer
        assert buf is not None
        # Still well inside the window -> no flush, nothing scheduled.
        gh.record_request_duration(0.06)
        assert scheduled == []

        # Push the buffer's own clock back past the window boundary — the
        # SAME "time elapsed since last flush" check the real buffer runs.
        buf._last_flush = _time.time() - gh._WINDOW_S - 1
        gh.record_request_duration(0.07)
        await asyncio.sleep(0)  # let the scheduled background task run
        assert len(scheduled) == 1
        assert scheduled[0]["samples"] >= 1

    asyncio.run(_drive())


def test_health_trend_buffer_flush_reports_real_sample_bounds():
    """`HealthTrendBuffer._flush` also carries the REAL min/max sample
    timestamps (`start_at`/`end_at`) `gateway_health` depends on — additive,
    proven directly against the shared kernel it wires.
    """
    buf = h.HealthTrendBuffer(window_s=100, max_samples=5000)
    t0 = 42.0
    assert buf.add(1.0, at=t0) is None
    assert buf.add(2.0, at=t0 + 5) is None
    out = buf.add(3.0, at=t0 + 101)
    assert out is not None
    assert out["start_at"] == t0
    assert out["end_at"] == t0 + 101
