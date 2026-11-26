"""The host starts the curator scheduler and it claims the lease (ARBORA-OPS-R958)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.service_caller import ServiceCallers
from arbora.knowledge.curator_scheduler import build_curator_scheduler
from arbora.mcp_server.background import BackgroundHost


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.waits: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.t += seconds


class _Session:
    tenant = "t1"


class _Runtime:
    def __init__(self) -> None:
        self.scopes: list[frozenset[str]] = []

    @asynccontextmanager
    async def as_service(self, tenant: str, *, required_scopes: frozenset[str]) -> Any:
        self.scopes.append(required_scopes)
        yield object()


async def _invoke(*_a: Any) -> Any:
    raise AssertionError("the curator is not due: no agent run")


def _callers(runtime: _Runtime) -> ServiceCallers:
    return ServiceCallers(
        process_session=lambda: _Session(), runtime=runtime, graph="g", invoke=_invoke
    )


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_service_caller_serves_the_process_tenant_only() -> None:
    callers = _callers(_Runtime())
    assert list(await callers.tenants()) == ["t1"]
    with pytest.raises(OperationRefused) as refused:
        await callers.send("other", "Loops", {}, scopes=frozenset({"loops:control"}))
    assert refused.value.code == "NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R958")
async def test_host_scheduler_ticks_on_the_injected_clock_and_claims_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any], frozenset[str]]] = []

    async def fake_send(
        _client: Any, method: str, params: dict[str, Any], _graph: str, *, key: Any
    ) -> Any:
        sent.append((method, params["op"], frozenset()))
        if method == "Loops" and params["op"]["op"] == "claim":
            return {"instance": {"lease": {"epoch": 4}}}
        if method == "KnowledgeCuration":
            return {"due": False}
        return {}

    monkeypatch.setattr("arbora.api.invoke.service_caller.send_on_client", fake_send)
    runtime, clock = _Runtime(), FakeClock()
    scheduler = build_curator_scheduler(_callers(runtime), clock, tick_s=30.0)
    stops = iter([False, False, True])
    await scheduler.run(lambda: next(stops))
    assert clock.waits == [30.0, 30.0]
    methods = [(m, op["op"]) for m, op, _ in sent]
    assert methods[:3] == [
        ("Loops", "claim"),
        ("KnowledgeCuration", "inputs"),
        ("Loops", "release"),
    ]
    assert sent[0][1]["graph"] == "g"
    assert runtime.scopes[0] == frozenset({"loops:control"})
    assert runtime.scopes[1] == frozenset({"knowledge:curate"})


@pytest.mark.spec("ARBORA-OPS-R958")
def test_background_host_runs_a_service_until_stopped() -> None:
    import threading

    started = threading.Event()

    class Service:
        async def run(self, should_stop: Any) -> None:
            import asyncio

            started.set()
            while not should_stop():
                await asyncio.sleep(0)

    host = BackgroundHost([Service()])
    host.start()
    assert started.wait(5)
    host.stop()
