"""The agent-run delegation fields are guarded through the medulla pin-lag module.

Against the pinned release (a delegation record without ``kind``, ``run_id`` and
``operations``) recording refuses ``UNAVAILABLE`` naming the field and sends
nothing. Against the engine ``main`` shape (a structural double) the real path
records the one ``agent_run`` delegation the engine expects (ARBORA-A2A-R001.2.2).
"""

from __future__ import annotations

import asyncio

import pytest

from arbora.a2a.authority import _RUN_LEASE_SECONDS, WorkItemA2AAuthority
from arbora.a2a.run_delegation import (
    EngineRunDelegations,
    engine_run_delegations,
    run_delegation_id,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.core.settings import RuntimeConfig
from arbora.pin_lag import medulla
from arbora.pin_lag.unavailable import PinLagUnavailable
from arbora.run_origin import RunOriginRegistry
from arbora.security.session import use_session
from tests.a2a._agent_run_delegation import (
    DelegationStore,
    ReclaimWorkItems,
    install_agent_run_models,
    install_pinned_models,
)
from tests.a2a.test_operation_bridge import session
from tests.test_agents_run_end_to_end import FakeEngine

TASK = "a2a-" + "c" * 64


@pytest.mark.spec("ARBORA-A2A-R001.2.2")
def test_the_pinned_release_refuses_naming_the_missing_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pinned_models(monkeypatch)
    with pytest.raises(PinLagUnavailable) as missing:
        medulla.require_agent_run_delegation()
    assert missing.value.code == "UNAVAILABLE"
    assert missing.value.missing in {"DelegationKind", "DelegationRecord.kind"}
    assert medulla.agent_run_delegation_served() is False
    assert engine_run_delegations(lambda: None, clock=lambda: 0.0, lease_s=1.0) is None
    store = DelegationStore()
    recorder = EngineRunDelegations(store.send, clock=lambda: 0.0, lease_s=1.0)
    with pytest.raises(PinLagUnavailable):
        asyncio.run(
            recorder.record(
                tenant="tenant:one",
                principal="person:one",
                task_id=TASK,
                run_id=TASK,
                operations=("agent:helper",),
            )
        )
    assert store.sent == []  # nothing fell back to a local record


@pytest.mark.spec("ARBORA-A2A-R001.2.2")
def test_admission_on_the_pinned_release_is_refused_unavailable_and_admits_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_pinned_models(monkeypatch)
    engine = FakeEngine()
    store = DelegationStore()
    engine.work_items = ReclaimWorkItems(store)
    authority = WorkItemA2AAuthority(
        lambda: engine,
        origins=RunOriginRegistry(),
        delegations=EngineRunDelegations(
            store.send, clock=lambda: 0.0, lease_s=_RUN_LEASE_SECONDS
        ),
        config=RuntimeConfig(),
    )

    async def admit() -> None:
        with use_session(session(frozenset({"kg:read", "kg:write"}))):
            await authority._with_delegation(
                {},
                engine,
                session=session(),
                task_id=TASK,
                agent_name="helper",
            )

    with pytest.raises(OperationRefused) as refused:
        asyncio.run(admit())
    assert refused.value.code == "UNAVAILABLE"
    assert engine.work_items.rows == {} and store.sent == []


@pytest.mark.spec("ARBORA-A2A-R001.2.2")
def test_the_main_shape_records_one_agent_run_delegation_through_the_real_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_agent_run_models(monkeypatch)
    medulla.require_agent_run_delegation()
    assert medulla.agent_run_delegation_served() is True
    store = DelegationStore()
    engine = FakeEngine()
    engine.work_items = ReclaimWorkItems(store)
    now = 1_000.0
    authority = WorkItemA2AAuthority(
        lambda: engine,
        origins=RunOriginRegistry(),
        delegations=EngineRunDelegations(
            store.send, clock=lambda: now, lease_s=_RUN_LEASE_SECONDS
        ),
        config=RuntimeConfig(),
    )

    async def admit() -> dict[str, object]:
        who = session(frozenset({"kg:read", "kg:write"}))
        with use_session(who):
            return await authority._with_delegation(
                {"a": 1}, engine, session=who, task_id=TASK, agent_name="helper"
            )

    metadata = asyncio.run(admit())
    delegation_id = run_delegation_id(TASK)
    assert metadata == {"a": 1, "delegation_id": delegation_id}
    [sent] = store.sent
    assert sent["family"] == "delegation" and sent["op"] == "record"
    assert sent["request"]["work_item_id"] == delegation_id
    record = sent["request"]["record"]
    assert record["kind"] == "agent_run" and record["run_id"] == TASK
    assert record["operations"] == ["agent:helper"] and record["tool"] == ""
    assert record["tenant"] == "tenant:one" and record["principal"] == "person:one"
    assert record["expires_at"] == int(now * 1000) + int(_RUN_LEASE_SECONDS * 3000)
