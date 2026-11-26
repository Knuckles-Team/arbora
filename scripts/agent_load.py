"""Qualify the served ``agents.run`` path under load (ARBORA-CAPACITY-R004).

    python scripts/agent_load.py --base-url URL --message-file FILE \
        [--requests 1000] [--levels 1,8,32] [--p95-bound-ms 2000]

The bearer token is read from ``ARBORA_LOAD_TOKEN``. At each concurrency the
driver starts ``--requests`` runs through the served ``agents.run`` operation,
waits for each run's terminal state through ``agents.tasks.get`` and records
the terminal latency. A run that passes ``--timeout-s`` (30 s) counts as a
failure. The report names the workload, the fixture and the owner revisions
and holds one verdict per level. A level with a missing sample, or a run with
no fixture or owner revisions named, is refused with ``QUALIFICATION_REFUSED``:
a report built from bounded-map or scorer-only data cannot qualify the path.
Exit 0 when every level passes, 1 on a failed level, 2 on a refusal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

LEVELS = (1, 8, 32)
REQUESTS = 1000
P95_BOUND_MS = 2000.0
ERROR_RATE_BOUND = 0.01
TIMEOUT_S = 30.0
QUALIFICATION_REFUSED = "QUALIFICATION_REFUSED"

#: One run to its terminal state: ``True`` when it ended well, else ``False``.
#: A timeout raises ``TimeoutError``; any other exception is a failure.
RunOnce = Callable[[], Awaitable[bool]]
Clock = Callable[[], float]


class QualificationRefused(Exception):
    """The inputs cannot support a verdict; ``reasons`` names each gap."""

    code = QUALIFICATION_REFUSED

    def __init__(self, reasons: list[str]) -> None:
        super().__init__(f"{QUALIFICATION_REFUSED}: {', '.join(reasons)}")
        self.reasons = reasons


def p95(values: Sequence[float]) -> float:
    """Nearest-rank 95th percentile; the caller guarantees ``values`` is not empty."""
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, round(0.95 * len(ordered)) - 1))
    return ordered[rank]


async def measure(
    run_once: RunOnce,
    *,
    concurrency: int,
    requests: int,
    clock: Clock = time.monotonic,
) -> list[tuple[float, bool]]:
    """``requests`` runs, at most ``concurrency`` at a time: (latency_ms, ok)."""
    gate = asyncio.Semaphore(concurrency)
    samples: list[tuple[float, bool]] = []

    async def one() -> None:
        async with gate:
            started = clock()
            try:
                ok = await run_once()
            except Exception:  # a timeout or any error is one failed run
                ok = False
            samples.append(((clock() - started) * 1000.0, ok))

    await asyncio.gather(*(one() for _ in range(requests)))
    return samples


def level_report(
    concurrency: int,
    requests: int,
    samples: Sequence[tuple[float, bool]],
    *,
    p95_bound_ms: float = P95_BOUND_MS,
    error_bound: float = ERROR_RATE_BOUND,
) -> dict[str, Any]:
    """One level's measurements and its verdict; a short sample list refuses."""
    if len(samples) != requests or requests < 1:
        raise QualificationRefused([f"missing_samples:{concurrency}"])
    failures = sum(1 for _, ok in samples if not ok)
    latency = p95([ms for ms, _ in samples])
    rate = failures / requests
    passed = latency <= p95_bound_ms and rate <= error_bound
    return {
        "concurrency": concurrency,
        "requests": requests,
        "p95_ms": round(latency, 3),
        "error_rate": rate,
        "verdict": "pass" if passed else "fail",
    }


def qualify(
    levels: Sequence[dict[str, Any]],
    *,
    workload: str,
    fixture: str,
    owner_revisions: dict[str, str],
) -> dict[str, Any]:
    """The qualification report: refused unless every input is named."""
    gaps = [
        name
        for name, present in (
            ("workload", bool(workload)),
            ("fixture", bool(fixture)),
            (
                "owner_revisions",
                bool(owner_revisions) and all(owner_revisions.values()),
            ),
            ("levels", bool(levels)),
        )
        if not present
    ]
    if gaps:
        raise QualificationRefused([f"missing_{gap}" for gap in gaps])
    passed = all(level["verdict"] == "pass" for level in levels)
    return {
        "workload": workload,
        "fixture": fixture,
        "owner_revisions": dict(owner_revisions),
        "levels": list(levels),
        "verdict": "pass" if passed else "fail",
    }


async def profile(
    run_once: RunOnce,
    *,
    levels: Sequence[int] = LEVELS,
    requests: int = REQUESTS,
    workload: str,
    fixture: str,
    owner_revisions: dict[str, str],
    clock: Clock = time.monotonic,
) -> dict[str, Any]:
    reports = []
    for concurrency in levels:
        samples = await measure(
            run_once, concurrency=concurrency, requests=requests, clock=clock
        )
        reports.append(level_report(concurrency, requests, samples))
    return qualify(
        reports, workload=workload, fixture=fixture, owner_revisions=owner_revisions
    )


def _connection(base_url: str) -> tuple[Any, Any]:
    """The parsed base URL and the connection class for its checked scheme."""
    import http.client
    import urllib.parse

    base = urllib.parse.urlsplit(base_url.rstrip("/"))
    if base.scheme not in {"http", "https"} or not base.hostname:
        raise SystemExit("--base-url must be an http or https URL")
    if base.scheme == "https":
        return base, http.client.HTTPSConnection
    return base, http.client.HTTPConnection


def _served_run(args: argparse.Namespace) -> RunOnce:
    token = os.environ["ARBORA_LOAD_TOKEN"]
    with open(args.message_file, encoding="utf-8") as handle:
        message = json.loads(handle.read())
    base, connect = _connection(args.base_url)

    def post(op: str, body: dict[str, Any]) -> dict[str, Any]:
        connection = connect(base.hostname, base.port, timeout=args.timeout_s)
        try:
            connection.request(
                "POST",
                f"{base.path}/api/v1/ops/{op}",
                body=json.dumps(body),
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": os.urandom(12).hex(),
                },
            )
            return json.loads(connection.getresponse().read())
        finally:
            connection.close()

    async def once() -> bool:
        deadline = time.monotonic() + args.timeout_s
        started = await asyncio.to_thread(post, "agents.run", {"message": message})
        task_id = (started.get("result") or {}).get("task_id")
        while task_id and time.monotonic() < deadline:
            task = await asyncio.to_thread(
                post, "agents.tasks.get", {"task_id": task_id}
            )
            state = str((task.get("result") or {}).get("state", ""))
            if state in {"completed", "failed", "canceled", "rejected"}:
                return state == "completed"
            await asyncio.sleep(0.05)
        raise TimeoutError

    return once


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--message-file", required=True)
    parser.add_argument("--requests", type=int, default=REQUESTS)
    parser.add_argument("--levels", default=",".join(str(n) for n in LEVELS))
    parser.add_argument("--timeout-s", type=float, default=TIMEOUT_S)
    parser.add_argument("--workload", default="agents.run terminal latency")
    parser.add_argument("--fixture", default="")
    parser.add_argument("--owner-revision", action="append", default=[])
    args = parser.parse_args(argv)
    revisions = dict(item.split("=", 1) for item in args.owner_revision if "=" in item)
    try:
        report = asyncio.run(
            profile(
                _served_run(args),
                levels=[int(n) for n in args.levels.split(",")],
                requests=args.requests,
                workload=args.workload,
                fixture=args.fixture,
                owner_revisions=revisions,
            )
        )
    except QualificationRefused as refused:
        sys.stdout.write(
            json.dumps(
                {"verdict": "refused", "code": refused.code, "reasons": refused.reasons}
            )
            + "\n"
        )
        return 2
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
