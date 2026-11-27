"""The resource-grant ledger is engine-held and bounds index jobs (ARBORA-OPS-R073)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.ingest import service
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import resource_grants as _grants
from tests.api._lane_double import EngineDouble, context

pytestmark = pytest.mark.spec("ARBORA-OPS-R073")


class _Kv:
    """An engine KV with versioned compare-and-set, shared across hosts."""

    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], tuple[bytes, int]] = {}

    def get(self, params: dict[str, Any]) -> Any:
        found = self.entries.get((params["namespace"], params["key"]))
        return {} if found is None else {"value": found[0], "version": found[1]}

    def cas(self, params: dict[str, Any]) -> Any:
        key = (params["namespace"], params["key"])
        version = self.entries.get(key, (b"", 0))[1]
        if params["expected_version"] != version:
            return {"swapped": False, "version": version}
        self.entries[key] = (params["new"], version + 1)
        return {"swapped": True, "version": version + 1}

    def held(self, tenant: str = "tenant-a") -> dict[str, int]:
        raw = self.entries.get((_grants.NAMESPACE, tenant))
        return json.loads(raw[0])["held"] if raw else {}


class _Runner:
    def __init__(self, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.calls: list[dict[str, Any]] = []

    async def index_repository(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.fail is not None:
            raise self.fail
        return SimpleNamespace(model_dump=lambda mode: {"job_id": "j"})


def _ctx(kv: _Kv, monkeypatch: pytest.MonkeyPatch, runner: _Runner) -> Any:
    client = EngineDouble(KvGet=kv.get, KvCas=kv.cas)
    ctx = context(client, monkeypatch, services={"ingest_runner": runner})
    monkeypatch.setattr(service, "_bound_runner", lambda _ctx: runner)
    return ctx


@pytest.mark.asyncio
async def test_grant_recorded_refused_and_released(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kv = _Kv()
    runner = _Runner()
    ctx = _ctx(kv, monkeypatch, runner)
    params = {"repo_id": "r", "max_working_bytes": 1_048_576}
    answer = await service.index_repository(ctx, params, None)
    assert answer == {"value": {"job_id": "j"}}
    assert kv.held() == {}  # released on success: back at baseline
    assert runner.calls[0]["max_working_bytes"] == 1_048_576
    # An over-grant request refuses before the job is admitted, and releases.
    too_big = {"repo_id": "r", "max_working_bytes": 8_589_934_593}
    with pytest.raises(OperationRefused) as refused:
        await service.index_repository(ctx, too_big, None)
    assert refused.value.code == "LOAD_CAP_EXCEEDED"
    assert len(runner.calls) == 1 and kv.held() == {}
    # A failing job releases its grant too.
    failing = _Runner(fail=RuntimeError("boom"))
    ctx = _ctx(kv, monkeypatch, failing)
    with pytest.raises(RuntimeError):
        await service.index_repository(ctx, params, None)
    assert kv.held() == {}


@pytest.mark.asyncio
async def test_ledger_is_engine_held_keyed_by_tenant_and_shared_across_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kv = _Kv()
    first = _grants.ResourceGrantLedger(
        context(EngineDouble(KvGet=kv.get, KvCas=kv.cas), monkeypatch), limit=100
    )
    await first.admit("a", 60)
    # A second host with no memory of the first sees the held grant in the engine.
    second = _grants.ResourceGrantLedger(
        context(EngineDouble(KvGet=kv.get, KvCas=kv.cas), monkeypatch), limit=100
    )
    with pytest.raises(OperationRefused) as refused:
        await second.admit("b", 60)
    assert refused.value.code == "LOAD_CAP_EXCEEDED"
    await second.admit("b", 40)
    assert kv.held() == {"a": 60, "b": 40}
    other = _grants.ResourceGrantLedger(
        context(EngineDouble(KvGet=kv.get, KvCas=kv.cas), monkeypatch, tenant="t2"),
        limit=100,
    )
    await other.admit("a", 100)  # another tenant has its own ledger
    await first.release("a")
    await first.release("a")  # releasing twice is not an error
    assert kv.held() == {"b": 40} and kv.held("t2") == {"a": 100}


@pytest.mark.asyncio
async def test_a_lost_compare_and_set_is_retried_and_a_contended_ledger_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kv = _Kv()
    lose = {"n": 1}

    def cas(params: dict[str, Any]) -> Any:
        if lose["n"] > 0:
            lose["n"] -= 1
            return {"swapped": False, "version": 9}
        return kv.cas(params)

    ledger = _grants.ResourceGrantLedger(
        context(EngineDouble(KvGet=kv.get, KvCas=cas), monkeypatch), limit=10
    )
    await ledger.admit("a", 5)
    assert kv.held() == {"a": 5}
    lose["n"] = 99
    with pytest.raises(OperationRefused) as refused:
        await ledger.admit("b", 1)
    assert refused.value.code == "REVISION_CONFLICT"
    with pytest.raises(OperationRefused):
        await ledger.admit("c", 0)
