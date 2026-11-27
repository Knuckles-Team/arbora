"""Per-subsystem health of the served surface (ARBORA-FLEET-R065).

The served intent surface rests on three subsystems that fail on their own:
the engine (``ask`` and every registry operation), discovery (``find``) and
the fleet gateway (``act`` with ``fleet.*``). One tracker records every call's
outcome and latency per subsystem, so a wedge in one shows as that subsystem
``wedged`` (a call in flight past the wedge bound) or ``degraded`` (recent
failures), never as "MCP down".

Exposed as Prometheus series ``arbora_subsystem_up{subsystem}``,
``arbora_subsystem_in_flight{subsystem}`` and
``arbora_subsystem_call_duration_seconds{subsystem,outcome}``, and as the
unauthenticated ``GET /health/subsystems`` body (names and states only).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from typing import Any

from arbora.observability.gateway_metrics import _gauge, _histogram
from arbora.observability.state import SUBSYSTEMS, SubsystemWindows

__all__ = [
    "WEDGE_AFTER_S",
    "classify",
    "snapshot",
    "track",
]

#: A call in flight longer than this marks its subsystem wedged.
WEDGE_AFTER_S = 30.0

SUBSYSTEM_UP = _gauge(
    "arbora_subsystem_up",
    "1 when the served subsystem is ok, 0 when degraded or wedged",
    ("subsystem",),
)
SUBSYSTEM_IN_FLIGHT = _gauge(
    "arbora_subsystem_in_flight",
    "Served calls in flight per subsystem",
    ("subsystem",),
)
SUBSYSTEM_DURATION = _histogram(
    "arbora_subsystem_call_duration_seconds",
    "Served call latency per subsystem and outcome",
    ("subsystem", "outcome"),
)


def classify(verb: str, op: str | None) -> str:
    """The subsystem one served call exercises."""

    if verb == "find":
        return "discovery"
    if op and op.startswith("fleet."):
        return "fleet"
    return "engine"


def _state(
    started: list[float], recent: list[tuple[bool, float]], now: float
) -> dict[str, Any]:
    oldest = max((now - t for t in started), default=0.0)
    failures = sum(1 for ok, _ in recent if not ok)
    if oldest > WEDGE_AFTER_S:
        status = "wedged"
    elif recent and failures * 2 > len(recent):
        status = "degraded"
    else:
        status = "ok"
    latencies = sorted(d for _, d in recent)
    p99 = (
        latencies[min(len(latencies) - 1, int(len(latencies) * 0.99))]
        if latencies
        else None
    )
    return {
        "status": status,
        "in_flight": len(started),
        "oldest_in_flight_s": round(oldest, 3),
        "recent_calls": len(recent),
        "recent_failures": failures,
        "recent_p99_s": None if p99 is None else round(p99, 3),
    }


def snapshot(windows: SubsystemWindows) -> dict[str, Any]:
    """Each subsystem's state; also refreshes the ``arbora_subsystem_up`` gauge."""

    now = time.monotonic()
    states = {
        name: _state(started, recent, now)
        for name, (started, recent) in windows.view(SUBSYSTEMS).items()
    }
    for name, state in states.items():
        SUBSYSTEM_UP.labels(subsystem=name).set(1 if state["status"] == "ok" else 0)
    overall = "ok" if all(s["status"] == "ok" for s in states.values()) else "degraded"
    return {"status": overall, "subsystems": states}


@contextlib.contextmanager
def track(windows: SubsystemWindows, subsystem: str) -> Iterator[dict[str, bool]]:
    """Record one served call; set ``outcome["ok"] = False`` for a refusal."""

    outcome = {"ok": True}
    start = time.monotonic()
    call_id = windows.begin(subsystem, start)
    SUBSYSTEM_IN_FLIGHT.labels(subsystem=subsystem).inc()
    try:
        yield outcome
    except BaseException:
        outcome["ok"] = False
        raise
    finally:
        elapsed = time.monotonic() - start
        windows.end(subsystem, call_id, ok=outcome["ok"], elapsed=elapsed)
        SUBSYSTEM_IN_FLIGHT.labels(subsystem=subsystem).dec()
        SUBSYSTEM_DURATION.labels(
            subsystem=subsystem, outcome="ok" if outcome["ok"] else "error"
        ).observe(elapsed)
