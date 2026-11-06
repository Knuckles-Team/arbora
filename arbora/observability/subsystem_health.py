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
import threading
import time
from collections import deque
from collections.abc import Iterator
from typing import Any

from arbora.observability.gateway_metrics import _gauge, _histogram

__all__ = [
    "SUBSYSTEMS",
    "WEDGE_AFTER_S",
    "classify",
    "snapshot",
    "track",
]

SUBSYSTEMS = ("engine", "discovery", "fleet")

#: A call in flight longer than this marks its subsystem wedged.
WEDGE_AFTER_S = 30.0
#: Recent outcomes kept per subsystem.
_WINDOW = 50

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

_lock = threading.Lock()
_in_flight: dict[str, dict[int, float]] = {name: {} for name in SUBSYSTEMS}
_recent: dict[str, deque[tuple[bool, float]]] = {
    name: deque(maxlen=_WINDOW) for name in SUBSYSTEMS
}
_next_id = 0


def classify(verb: str, op: str | None) -> str:
    """The subsystem one served call exercises."""

    if verb == "find":
        return "discovery"
    if op and op.startswith("fleet."):
        return "fleet"
    return "engine"


def _state(name: str, now: float) -> dict[str, Any]:
    started = list(_in_flight[name].values())
    oldest = max((now - t for t in started), default=0.0)
    recent = list(_recent[name])
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


def snapshot() -> dict[str, Any]:
    """Each subsystem's state; also refreshes the ``arbora_subsystem_up`` gauge."""

    now = time.monotonic()
    with _lock:
        states = {name: _state(name, now) for name in SUBSYSTEMS}
    for name, state in states.items():
        SUBSYSTEM_UP.labels(subsystem=name).set(1 if state["status"] == "ok" else 0)
    overall = "ok" if all(s["status"] == "ok" for s in states.values()) else "degraded"
    return {"status": overall, "subsystems": states}


@contextlib.contextmanager
def track(subsystem: str) -> Iterator[dict[str, bool]]:
    """Record one served call; set ``outcome["ok"] = False`` for a refusal."""

    global _next_id
    outcome = {"ok": True}
    start = time.monotonic()
    with _lock:
        _next_id += 1
        call_id = _next_id
        _in_flight[subsystem][call_id] = start
    SUBSYSTEM_IN_FLIGHT.labels(subsystem=subsystem).inc()
    try:
        yield outcome
    except BaseException:
        outcome["ok"] = False
        raise
    finally:
        elapsed = time.monotonic() - start
        with _lock:
            _in_flight[subsystem].pop(call_id, None)
            _recent[subsystem].append((outcome["ok"], elapsed))
        SUBSYSTEM_IN_FLIGHT.labels(subsystem=subsystem).dec()
        SUBSYSTEM_DURATION.labels(
            subsystem=subsystem, outcome="ok" if outcome["ok"] else "error"
        ).observe(elapsed)
