"""The host drives one harness-evolution tick per completed run (ARBORA-HOST-R035)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest
from meristem.graph import RunBudget
from meristem.graph.harness_cycle import CycleOutcome
from meristem.host_ports import HostPorts

from arbora import agent_runs
from arbora.agent_host.harness_cycle import (
    COMPONENTS_OP,
    ENVELOPE_OP,
    HostCycleProvider,
    HostProbeProvider,
)
from arbora.agent_host.host_port_adapters import HostPortsBinding, HostPortsSlot
from arbora.api.contracts.agent_control_contracts import AgentExecutionRequest
from arbora.mcp_server import harness_ports
from arbora.mcp_server.host_context import build_host_context
from arbora.run_origin import RunOriginRegistry
from arbora.security.session import ActorContext, GraphSession


def _session() -> GraphSession:
    return GraphSession(
        actor=ActorContext(actor_id="a", tenant_id="t1", authenticated=True),
        tenant="t1",
        scopes=frozenset({"kg:write"}),
    )


@contextmanager
def _native(session: GraphSession, run_id: str):
    with agent_runs.native_run_scope(
        run_id=run_id,
        task_id=f"task:{run_id}",
        context_id=f"context:{run_id}",
        work_item_id=f"work:{run_id}",
        session=session,
        claim={
            "claimed": True,
            "tenant": session.tenant,
            "lease_holder_ref": "worker",
            "lease_epoch": 1,
            "fencing_token": 1,
        },
    ):
        yield


class _Executor:
    async def execute_agent(self, request: Any, **_kw: Any) -> Any:
        return SimpleNamespace(
            run_id=request.run_id, output="done", execution_mode="graph", usage=None
        )


class _Cycle:
    """The registered cycle provider: records each tick, answers an abstention."""

    def __init__(self, error: Exception | None = None) -> None:
        self.ticks: list[str] = []
        self.error = error

    async def __call__(self, request: Any, plan: Any, **_kw: Any) -> CycleOutcome:
        self.ticks.append(request.run_id)
        if self.error is not None:
            raise self.error
        return CycleOutcome(
            task_ref=request.run_id,
            k=3,
            adopted_version_ref=None,
            abstain_reason="no_candidates",
            contrast_ref=None,
            lesson_ref=None,
        )


def _slot(cycle: _Cycle) -> HostPortsSlot:
    slot = HostPortsSlot()
    slot.bind(HostPortsBinding(HostPorts(cycle=cycle), None))
    return slot


def _plan(slots: int | None, minimum: int | None = None) -> Any:
    return SimpleNamespace(
        budget=RunBudget(candidate_slots=slots, candidate_slots_min=minimum)
    )


async def _run(
    port: Any, session: GraphSession, run_id: str
) -> tuple[Any, tuple[asyncio.Task[None], ...]]:
    with _native(session, run_id):
        result = await port.execute_agent(
            AgentExecutionRequest(agent_name="helper", task="work", run_id=run_id),
            session=session,
        )
    ticks = port.pending_ticks
    await asyncio.gather(*ticks)
    return result, ticks


@pytest.mark.spec("ARBORA-HOST-R035")
async def test_completed_run_drives_one_tick_and_probes_drive_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = _session()
    cycle = _Cycle()
    plans = {"run-1": _plan(3), "run-2": _plan(1), "run-3": _plan(4, 5)}
    monkeypatch.setattr(agent_runs, "decide_run", lambda req: plans[req.run_id])
    port = agent_runs.AgentRunPort(
        _Executor(), origins=RunOriginRegistry(), ports=_slot(cycle)
    )

    first, ticks = await _run(port, session, "run-1")
    assert first.run_id == "run-1" and len(ticks) == 1
    assert cycle.ticks == ["run-1"]

    # Slots below the plan's own minimum, and a plan holding none, drive no tick.
    await _run(port, session, "run-2")
    await _run(port, session, "run-3")
    assert cycle.ticks == ["run-1"]

    # A probe run drives none: the probe provider runs through execute_graph only.
    import meristem.graph as graph

    async def fake_execute_graph(_graph: Any, _config: Any, task: str, **kw: Any):
        return {
            "run_id": kw["run_id"],
            "status": "completed",
            "steps": 2,
            "usage": {"requests": 1},
            "error": None,
        }

    monkeypatch.setattr(graph, "execute_graph", fake_execute_graph)
    probe = HostProbeProvider(lambda: HostPorts(cycle=cycle))
    trace = await probe(
        graph=None,
        config={},
        task="work",
        run_id="run-1:probe:current",
        mode="ask",
        requested_model_id="m",
        budget_ref="b",
        agent_name="helper",
        metadata={"harness_probe": {"candidate_id": "current", "seed": "s"}},
    )
    assert trace["run_id"] == "run-1:probe:current"
    assert trace["status"] == "completed" and trace["steps"] == 2
    from meristem.graph.harness_cycle import conditions_digest

    assert trace["conditions_digest"] == conditions_digest(
        task="work", seed="s", model_ref="m", budget_ref="b"
    )
    assert (
        agent_runs._tick_due({"harness_probe": {"candidate_id": "x"}}, _plan(5))
        is False
    )
    assert cycle.ticks == ["run-1"]

    # A tick failure logs and leaves the finished run result unchanged.
    failing = _Cycle(error=RuntimeError("engine down"))
    port = agent_runs.AgentRunPort(
        _Executor(), origins=RunOriginRegistry(), ports=_slot(failing)
    )
    with caplog.at_level(logging.WARNING, logger="arbora.agent_runs"):
        result, ticks = await _run(port, session, "run-1")
    assert (result.run_id, result.output) == ("run-1", "done")
    assert failing.ticks == ["run-1"] and len(ticks) == 1
    assert "harness cycle tick of run run-1 failed" in caplog.text


@pytest.mark.spec("ARBORA-HOST-R035")
async def test_cycle_provider_reads_the_library_and_writes_envelopes_as_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], str]] = []

    async def call(op_id: str, params: Any, run_ref: str, plan_ref: Any = None) -> Any:
        calls.append((op_id, dict(params), run_ref))
        if op_id == COMPONENTS_OP:
            rows = [
                {"kind": "system_prompt", "id": "v1"},
                {"kind": "skill", "id": "v2", "task_class": "other"},
                {"kind": "memory"},
            ]
            return {"outcome": "answered", "payload": {"value": {"components": rows}}}
        return {"outcome": "answered", "payload": {"value": "ok"}}

    seen: dict[str, Any] = {}

    async def fake_tick(request: Any, plan: Any, **kw: Any) -> CycleOutcome:
        seen["versions"] = list(kw["library_query"]("helper"))
        seen["ref"] = kw["write_envelope"]({"kind": "harness_adoption", "n": 1})
        return CycleOutcome(request.run_id, 3, "v1", None, "c", "l")

    import meristem.graph.harness_cycle as cycle_module

    monkeypatch.setattr(cycle_module, "run_cycle_tick", fake_tick)
    provider = HostCycleProvider(call)
    outcome = await provider(
        SimpleNamespace(run_id="run-9", agent_name="helper"),
        _plan(3),
        task_class="helper",
        graph=None,
    )
    assert outcome.adopted_version_ref == "v1"
    assert seen["versions"] == [
        {"component_kind": "system_prompt", "component_version_ref": "v1"}
    ]
    node = next(c for c in calls if c[0] == ENVELOPE_OP)
    assert node[2] == "run-9" and node[1]["node_id"] == seen["ref"]
    stored = msgpack.unpackb(bytes(node[1]["properties_msgpack"]), raw=False)
    assert stored == {"kind": "harness_adoption", "n": 1}


@pytest.mark.spec("ARBORA-HOST-R035")
def test_the_host_composes_both_providers_into_the_one_record() -> None:
    from arbora.agent_host import host_port_composition

    host = build_host_context(lambda: None)
    bare = harness_ports.build_host_ports(host)
    assert isinstance(bare.probe, HostProbeProvider)
    assert bare.cycle is None  # no served engine, no run-principal call to read through

    composed = harness_ports.build_host_ports(host, services=SimpleNamespace())
    assert isinstance(composed.cycle, HostCycleProvider)
    assert isinstance(composed.probe, HostProbeProvider)
    assert composed.harness_cycle_provider() is composed.cycle
    assert composed.harness_probe_provider() is composed.probe
    assert host_port_composition.HostCycleProvider is HostCycleProvider
