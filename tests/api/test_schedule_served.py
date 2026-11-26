"""A scheduled agent run over the real engine and the served MCP surface.

The owner runs this test against a booted engine (ARBORA-OPS-R022.2.10). It needs
the engine scheduler to tick; it polls the tick history with a bounded deadline
and never sleeps for a fixed time.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from tests.test_real_boot import (  # noqa: F401
    _mcp_session,
    _paced_native_read,
    _post_op_keyed,
    _structured,
    pytestmark,
    stack,
)

TICK_DEADLINE_S = 90.0


async def _ticks(client: Any, name: str) -> list[dict[str, Any]]:
    answer = _structured(
        await _paced_native_read(client, "schedules.runs", {"name": name, "limit": 20})
    )
    value = answer.get("result", answer).get("value", {})
    return list(value.get("ticks", []))


async def _until(client: Any, name: str, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + TICK_DEADLINE_S
    ticks: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        ticks = await _ticks(client, name)
        if len(ticks) >= count:
            return ticks
        await asyncio.sleep(2.0)
    raise AssertionError(f"schedule {name} reported {len(ticks)} ticks, wanted {count}")


@pytest.mark.spec("ARBORA-OPS-R022.2.10")
async def test_scheduled_agent_run_fires_pauses_and_reports_over_mcp(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    name = f"served-{int(time.time())}"
    _post_op_keyed(
        stack,
        "schedules.create",
        {
            "name": name,
            "task_kind": "agent_run",
            "spec": {"op": "agents.run", "message": "report ready"},
            "period_s": 1,
            "max_run_s": 30,
            "max_tokens": 1000,
        },
    )
    async with await _mcp_session(stack) as client:
        fired = await _until(client, name, 1)
        assert all(tick.get("outcome") for tick in fired)
        _post_op_keyed(stack, "schedules.pause", {"name": name})
        settled = len(await _ticks(client, name))
        await asyncio.sleep(3.0)
        assert len(await _ticks(client, name)) == settled
        _post_op_keyed(stack, "schedules.resume", {"name": name})
        resumed = await _until(client, name, settled + 1)
        assert len(resumed) > settled
