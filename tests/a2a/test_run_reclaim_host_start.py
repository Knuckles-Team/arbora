"""The served host starts the A2A run reclaim consumer and it reclaims an expired item.

The consumer is composed by the host's background services (ARBORA-A2A-R001.2.2): its
candidates are the engine's own WorkItem list for the tenant the host serves, under the
process authority's session asked for at each poll, never kept. Time is the injected
clock and the wait is injected; nothing sleeps.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import _RUN_LEASE_SECONDS
from arbora.a2a.run_reclaim import A2ARunReclaimService
from arbora.mcp_server.served_invocation import _background_services, _reclaim_service
from arbora.observability.runtime_health import reset_health_probes
from arbora.security.session import use_session
from tests.a2a.test_checkpoint_resume_served import (  # noqa: F401
    START,
    Host,
    _interrupted_run,
    _world,
    lenient_decode,
)
from tests.a2a.test_operation_bridge import session


def _runtime(host: Host) -> Any:
    return SimpleNamespace(bindings={"a2a": SimpleNamespace(authority=host.authority)})


@pytest.mark.spec("ARBORA-A2A-R001.2.2")
def test_host_background_services_include_the_reclaim_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    host = Host(engine, store, checkpoints, None, clock)
    monkeypatch.setenv("ARBORA_CURATOR_TICK_S", "0")
    # The host's other A2A consumer (the tool-task consumer) has its own poll.
    monkeypatch.setenv("ARBORA_A2A_TOOL_TASK_POLL_S", "0")

    def who() -> Any:
        return SimpleNamespace(tenant="tenant-a")

    try:
        served = _background_services(object(), _runtime(host), "g", who)
        assert [type(service) for service in served] == [A2ARunReclaimService]
        # A host that composes no A2A service, or turns the poll off, starts none.
        assert (
            _background_services(object(), SimpleNamespace(bindings={}), "g", object)
            == ()
        )
        monkeypatch.setenv("ARBORA_A2A_RECLAIM_POLL_S", "0")
        assert _background_services(object(), _runtime(host), "g", object) == ()
    finally:
        reset_health_probes()


@pytest.mark.spec("ARBORA-A2A-R001.2.2")
def test_started_service_reclaims_one_expired_item_from_the_engine_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, store, checkpoints, clock = _world(monkeypatch)
    who = session(frozenset({"kg:read", "kg:write", "identity:delegate"}))
    asked: list[int] = []

    def process_session() -> Any:
        asked.append(1)
        return who

    async def scenario() -> Any:
        with use_session(who):
            first = Host(engine, store, checkpoints, "write", clock)
            task_id = await _interrupted_run(first)
            second = Host(engine, store, checkpoints, None, clock)
            waits: list[float] = []
            stops = iter([False, False, True])

            async def wait(seconds: float) -> None:
                waits.append(seconds)
                # The lease lapses while the host waits between polls.
                clock.now = START + _RUN_LEASE_SECONDS + 1

            service = _reclaim_service(
                _runtime(second), process_session, clock=clock, sleep=wait
            )
            assert service is not None
            await service.run(lambda: next(stops))
            return task_id, second.executor.effects, waits

    task_id, effects, waits = asyncio.run(scenario())
    row = engine.work_items.rows[f"workitem:orchestrator:{task_id}"]
    # The first poll found nothing expired; the second reclaimed the one item.
    assert effects == ["notify"] and row["status"] == "succeeded"
    assert len(waits) == 2
    # The session was asked of the process authority at each poll, not held.
    assert len(asked) >= 2
