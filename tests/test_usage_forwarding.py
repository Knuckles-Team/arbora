"""Step usage events reach the usage ledger (ARBORA-OPS-R891)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.agent_host.usage_ledger import UsageLedger


class _Callers:
    def __init__(self, refuse_first: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.refuse_first = refuse_first

    async def send(self, tenant: str, method: str, params: Any, **kw: Any) -> Any:
        self.calls.append({"tenant": tenant, "method": method, "params": params, **kw})
        if self.refuse_first and len(self.calls) == 1:
            error = RuntimeError("refused")
            error.code = "USAGE_QUERY_INVALID"  # type: ignore[attr-defined]
            raise error
        return {"appended": len(params["op"]["records"])}


def _event(run_id: str, step: int, **extra: Any) -> dict[str, Any]:
    return {
        "type": "step_usage",
        "run_id": run_id,
        "step": step,
        "model_key": "m",
        "tokens_in": 10,
        "tokens_out": 5,
        "tenant": "forged-tenant",
        **extra,
    }


@pytest.mark.spec("ARBORA-OPS-R891")
async def test_step_usage_events_append_once_per_run() -> None:
    callers = _Callers()
    ledger = UsageLedger()
    ledger.configure(callers, max_batch=2, now_ms=lambda: 7)
    ledger.bind("run-1", tenant="tenant:one", principal="principal:one")
    for step in range(5):
        ledger.forward(_event("run-1", step))
    ledger.forward({**_event("run-1", 9), "type": "tool_call"})
    ledger.forward(_event("run-unbound", 1))

    assert await ledger.flush("run-1") == 3
    assert [len(c["params"]["op"]["records"]) for c in callers.calls] == [2, 2, 1]
    first = callers.calls[0]
    assert first["method"] == "Usage" and first["scopes"] == frozenset({"usage:write"})
    assert first["tenant"] == "tenant:one"
    record = first["params"]["op"]["records"][0]
    assert record["tenant"] == "tenant:one" and record["principal"] == "principal:one"
    assert record["tokens_in"] == 10 and record["recorded_at_ms"] == 7
    assert {c["key"] for c in callers.calls} == {
        "usage:run-1:0",
        "usage:run-1:1",
        "usage:run-1:2",
    }
    assert await ledger.flush("run-1") == 0

    # A refusal never fails the run and never drops the other batches.
    refusing = _Callers(refuse_first=True)
    other = UsageLedger()
    other.configure(refusing, max_batch=1)
    other.bind("run-2", tenant="tenant:one", principal="p")
    for step in range(3):
        other.forward(_event("run-2", step))
    assert await other.flush("run-2") == 2
    assert len(refusing.calls) == 3
