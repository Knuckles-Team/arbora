"""Readiness snapshot (``graphos.readiness.v1``).

One collector joins independent probes into a single snapshot. A probe returns
a typed check; a probe that raises or overruns its deadline reports
``unavailable``, never ``ready``. One failed required check makes the whole
snapshot ``unavailable``; any other non-ready check degrades it. Every call
re-probes: a snapshot is never cached.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Literal

SCHEMA_VERSION = "graphos.readiness.v1"

State = Literal["ready", "degraded", "unavailable", "stale", "not_configured"]
Check = dict[str, Any]
Probe = Callable[[], Check]

REQUIRED_CHECKS: tuple[str, ...] = ("engine", "catalog", "synthetic_query")
_RANK = {"ready": 0, "not_configured": 0, "stale": 1, "degraded": 2, "unavailable": 3}


def check(state: State, **fields: Any) -> Check:
    return {"state": state, **{k: v for k, v in fields.items() if v is not None}}


def identity_check(session: Any = None) -> Check:
    """Ready only for a verified session; a bare name never counts as authority."""
    if session is None:
        return check("not_configured", reason="no verified session supplied")
    actor = getattr(session, "actor", None)
    if actor is not None and bool(getattr(actor, "authenticated", False)):
        return check("ready", carrier="verified")
    return check("degraded", carrier="unverified", reason="actor is not authenticated")


def _run(probe: Probe, pool: ThreadPoolExecutor, deadline_s: float) -> Check:
    future = pool.submit(probe)
    try:
        out = future.result(timeout=deadline_s)
    except FutureTimeout:
        future.cancel()
        return check("unavailable", reason=f"probe exceeded {deadline_s}s deadline")
    except Exception as exc:  # noqa: BLE001 - a broken probe is never ready
        return check("unavailable", reason=f"probe raised {type(exc).__name__}")
    return (
        out
        if out.get("state") in _RANK
        else check("unavailable", reason="bad probe result")
    )


def _rollup(
    checks: Mapping[str, Check], required: tuple[str, ...]
) -> tuple[State, list[str], list[str]]:
    failures = [n for n in required if checks.get(n, {}).get("state") == "unavailable"]
    missing = [n for n in required if n not in checks]
    failures += missing
    reasons = [
        n for n, c in checks.items() if _RANK[c["state"]] > 0 and n not in failures
    ]
    if failures:
        return "unavailable", failures, reasons
    return ("degraded" if reasons else "ready"), [], reasons


def collect_readiness_snapshot(
    probes: Mapping[str, Probe],
    *,
    required: tuple[str, ...] = REQUIRED_CHECKS,
    deadline_s: float = 5.0,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
) -> dict[str, Any]:
    """Run every probe concurrently and roll the results into one snapshot."""
    with ThreadPoolExecutor(max_workers=max(1, len(probes))) as pool:
        checks = {n: _run(p, pool, deadline_s) for n, p in probes.items()}
    overall, failures, reasons = _rollup(checks, required)
    observed = now().isoformat(timespec="seconds")
    digest = hashlib.sha256(
        json.dumps(
            {"checks": checks, "at": observed}, sort_keys=True, default=str
        ).encode()
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshot_id": digest[:16],
        "observed_at": observed,
        "overall": overall,
        "checks": checks,
        "required_failures": failures,
        "degraded_reasons": reasons,
    }


def is_snapshot_ready(snapshot: Mapping[str, Any]) -> bool:
    return snapshot.get("overall") == "ready"


_PROBES: dict[str, Probe] = {}


def register_probe(name: str, probe: Probe) -> None:
    """Add a named probe to the registry that readiness surfaces collect from."""
    _PROBES[name] = probe


def registered_probes() -> dict[str, Probe]:
    return dict(_PROBES)
