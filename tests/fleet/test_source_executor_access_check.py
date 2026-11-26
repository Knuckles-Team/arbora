"""Read-time access checks relayed to the connector (ARBORA-FLEET-R057)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.registry import Effect
from arbora.fleet.source_executor import (
    ACCESS_CHECK_BATCH,
    ACCESS_CHECK_UNAVAILABLE,
    FleetSourceExecutor,
)


class Gateway:
    def __init__(self, mode: str = "ok") -> None:
        self.mode = mode
        self.batches: list[list[str]] = []

    async def call(self, caller, server, tool, arguments, *, expected_effect):
        assert expected_effect is Effect.READ
        refs = list(arguments["record_refs"])
        self.batches.append(refs)
        if self.mode == "hang":
            await asyncio.Event().wait()
        if self.mode == "missing":
            raise LookupError("no such tool")
        if self.mode == "malformed":
            return {"value": {"oops": 1}}
        return {"value": {"decisions": {r: r.endswith("0") for r in refs}}}


def _run(gateway: Gateway, refs: list[str], **kw: Any) -> Any:
    executor = FleetSourceExecutor(gateway, object())
    return asyncio.run(executor.access_check("conn", "access_check", refs, **kw))


@pytest.mark.spec("ARBORA-FLEET-R057")
def test_access_check_batches_at_most_200_and_allows_only_named_true() -> None:
    refs = [f"r{i}" for i in range(450)]
    gateway = Gateway()
    result = _run(gateway, refs)
    assert result.error_code is None
    assert [len(b) for b in gateway.batches] == [ACCESS_CHECK_BATCH, 200, 50]
    assert result.allowed["r10"] is True and result.allowed["r11"] is False
    assert set(result.allowed) == set(refs)


@pytest.mark.spec("ARBORA-FLEET-R057")
def test_timeout_denies_every_record() -> None:
    refs = ["a0", "a1"]
    result = _run(Gateway("hang"), refs, timeout_s=0.01)
    assert result.error_code == ACCESS_CHECK_UNAVAILABLE
    assert result.allowed == {"a0": False, "a1": False}


@pytest.mark.spec("ARBORA-FLEET-R057")
@pytest.mark.parametrize("mode", ["missing", "malformed"])
def test_missing_tool_or_bad_reply_denies_every_record(mode: str) -> None:
    result = _run(Gateway(mode), ["x0"])
    assert result.error_code == ACCESS_CHECK_UNAVAILABLE
    assert result.allowed == {"x0": False}
