"""The HostPorts seams run through the served invoke path as the run principal (ARBORA-OPS-R890)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.agent_host.host_port_composition import compose_host_ports
from arbora.agent_runs import NativeRunContext
from arbora.security import request_identity
from tests.api._served_support import capture_invocation, person, route_methods
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.test_served_operation_api import served  # noqa: F401

RUN = "run:seam-1"


class Decide:
    """The loopback engine ``Decide``: records each request it answers."""

    def __init__(self) -> None:
        self.requests: list[tuple[dict[str, Any], str]] = []

    def __call__(self, params: dict[str, Any], principal: str) -> Any:
        self.requests.append((params, principal))
        return {"records": []}


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


def _owner() -> NativeRunContext:
    session = request_identity.mint_graph_session(
        person("one", extra_scopes=("query:decide",))
    )
    return NativeRunContext(
        run_id=RUN,
        task_id=RUN,
        context_id="ctx",
        work_item_id="work:1",
        session=session,
        claim={},
    )


@pytest.mark.spec("ARBORA-OPS-R890")
@pytest.mark.pin_lag
def test_engine_decision_seam_reaches_the_engine_as_the_run_principal(
    captured: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _http, client = served
    engine = Decide()
    route_methods(
        client,
        {"Decide": engine},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=("query:decide",))},
    )
    owner = _owner()
    binding = compose_host_ports(
        captured["invocation"].services,
        wait=lambda seconds: asyncio.sleep(0),
        resolve_run=lambda ref: owner if ref == RUN else None,
    )
    assert binding.refusal is None
    ports = binding.ports

    async def scenario() -> Any:
        answered = await ports.engine_decision.decide(
            "TaskPlan", {"question": "TaskPlan"}, run_ref=RUN
        )
        verdict = await ports.decision("BranchSelect", {}, RUN)
        foreign = await ports.engine_decision.decide("TaskPlan", {}, run_ref="run:x")
        return answered, verdict, foreign

    answered, verdict, foreign = asyncio.run(scenario())
    assert answered.outcome == "answered", answered
    # The run principal's own scopes gate the call: without one the invoke refuses.
    assert verdict.record_id == ""
    # Both calls reached the engine as the verified person that owns the run.
    assert [principal for _p, principal in engine.requests[:2]] == ["person:one"] * 2
    # A run that is not executing answers like an unknown one and reaches nothing.
    assert foreign.outcome == "refused" and foreign.refusal_code == "UNAVAILABLE"
    assert len(engine.requests) == 2


@pytest.mark.spec("ARBORA-OPS-R890")
def test_served_start_composes_the_record_or_names_the_pin_gap(
    captured: Any,
    served: Any,  # noqa: F811
) -> None:
    from meristem import host_ports

    from arbora import agent_runs

    agent_runs.bind_run_ports(captured["invocation"].services)
    binding = agent_runs.host_ports_binding()
    if hasattr(host_ports, "HostPorts"):
        assert binding.refusal is None and binding.ports.engine_decision is not None
    else:
        assert binding.ports is None
        assert binding.refusal.missing == "meristem.host_ports.HostPorts"
