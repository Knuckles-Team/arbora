"""Parallel find+act load probe against a served Arbora MCP endpoint (ARBORA-FLEET-R066).

    python scripts/load_find_act.py --url URL --workers 20 --seconds 120 \
        --fleet-server SERVER --fleet-tool TOOL [--p99-bound-s 10]

The bearer token is read from ``ARBORA_LOAD_TOKEN``. Each worker alternates
``find(intent=...)`` and ``act(op="fleet.call", ...)`` until the duration ends.
Every call has a hard per-call deadline (``--call-timeout-s``); a call that
passes it counts as a hang. Prints one JSON report and exits non-zero when any
call hangs or the p99 passes the bound.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from typing import Any


def percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of ``values`` (``None`` when empty)."""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(fraction * len(ordered))) - 1))
    return ordered[rank]


def summarize(samples: list[tuple[str, str, float]], bound_s: float) -> dict[str, Any]:
    """Per-kind counts and latency percentiles; ``ok`` holds the R066 verdict."""

    report: dict[str, Any] = {"p99_bound_s": bound_s, "kinds": {}}
    hangs = 0
    for kind in sorted({k for k, _, _ in samples}):
        rows = [(o, d) for k, o, d in samples if k == kind]
        durations = [d for _, d in rows]
        outcomes: dict[str, int] = {}
        for outcome, _ in rows:
            outcomes[outcome] = outcomes.get(outcome, 0) + 1
        hangs += outcomes.get("hang", 0)
        report["kinds"][kind] = {
            "calls": len(rows),
            "outcomes": outcomes,
            "p50_s": percentile(durations, 0.50),
            "p95_s": percentile(durations, 0.95),
            "p99_s": percentile(durations, 0.99),
            "max_s": max(durations, default=None),
        }
    p99 = percentile([d for _, _, d in samples], 0.99)
    report["p99_s"] = p99
    report["hangs"] = hangs
    report["ok"] = hangs == 0 and p99 is not None and p99 <= bound_s
    return report


def _outcome(result: Any) -> str:
    text = "".join(getattr(c, "text", "") for c in getattr(result, "content", []) or [])
    try:
        body = json.loads(text)
    except ValueError:
        return "error" if getattr(result, "is_error", False) else "ok"
    if isinstance(body, dict) and body.get("ok") is False:
        code = (
            (body.get("error") or {}).get("code")
            if isinstance(body.get("error"), dict)
            else None
        )
        return f"refused:{code or 'unknown'}"
    return "ok"


async def _worker(client: Any, args: argparse.Namespace, end: float, out: list) -> None:
    calls = [
        ("find", {"intent": args.intent}),
        (
            "act",
            {
                "op": "fleet.call",
                "params": {
                    "server": args.fleet_server,
                    "tool": args.fleet_tool,
                    "arguments": json.loads(args.fleet_arguments),
                },
                "execute": True,
            },
        ),
    ]
    turn = 0
    while time.monotonic() < end:
        kind, arguments = calls[turn % 2]
        turn += 1
        start = time.monotonic()
        try:
            result = await asyncio.wait_for(
                client.call_tool(kind, arguments, raise_on_error=False),
                timeout=args.call_timeout_s,
            )
            outcome = _outcome(result)
        except TimeoutError:
            outcome = "hang"
        except Exception as exc:  # noqa: BLE001 - every failure is a sample
            outcome = f"exception:{type(exc).__name__}"
        out.append((kind, outcome, time.monotonic() - start))


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from fastmcp import Client
    from fastmcp.client.auth import BearerAuth

    token = os.environ["ARBORA_LOAD_TOKEN"]
    samples: list[tuple[str, str, float]] = []
    clients = [Client(args.url, auth=BearerAuth(token)) for _ in range(args.workers)]
    for client in clients:
        await client.__aenter__()
    try:
        end = time.monotonic() + args.seconds
        await asyncio.gather(*(_worker(c, args, end, samples) for c in clients))
    finally:
        for client in clients:
            await client.__aexit__(None, None, None)
    report = summarize(samples, args.p99_bound_s)
    report.update(workers=args.workers, seconds=args.seconds)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--workers", type=int, default=20)
    parser.add_argument("--seconds", type=float, default=120.0)
    parser.add_argument("--intent", default="search the web")
    parser.add_argument("--fleet-server", required=True)
    parser.add_argument("--fleet-tool", required=True)
    parser.add_argument("--fleet-arguments", default="{}")
    parser.add_argument("--call-timeout-s", type=float, default=60.0)
    parser.add_argument("--p99-bound-s", type=float, default=10.0)
    args = parser.parse_args(argv)
    report = asyncio.run(run(args))
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
