"""The tool subset rides the harness carrier or is refused (ARBORA-A2A-R002.2.1).

The harness enforces ``HarnessConfig.action_space['tool_subset']`` itself (a call outside
it refuses ``TOOL_NOT_ALLOWED`` before the tool runs). The pinned harness lacks the
field, so ``pinned_harness`` is the real one and ``carrying_harness`` a structural double
of the field the harness ``main`` adds. Admission, the plan and the durable route are the
real ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest
from meristem.graph.state import RunRequest

from arbora import agent_runs
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import A2AAssemblyUnavailable
from arbora.a2a.tool_subset import (
    carrier_available,
    selected_tools_of,
    with_tool_subset,
)
from arbora.api.domain.a2a.models import A2ARouteDecision
from arbora.core.settings import RuntimeConfig
from arbora.run_origin import RunOriginRegistry
from tests.a2a.test_authority import _message
from tests.test_native_run_scope import SESSION, scope


@dataclass(frozen=True)
class CarryingHarness:
    """The harness ``main`` config: ``action_space`` carries the enforced subset."""

    response_format: str = "text"
    action_space: Mapping[str, Any] | None = field(default=None)


@pytest.fixture
def carrying_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    import meristem.graph.state as state

    monkeypatch.setattr(state, "HarnessConfig", CarryingHarness)


@dataclass(frozen=True)
class BareHarness:
    """A harness without the ``action_space`` field: it cannot enforce a subset."""

    response_format: str = "text"


@pytest.fixture
def bare_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    import meristem.graph.state as state

    monkeypatch.setattr(state, "HarnessConfig", BareHarness)


@dataclass(frozen=True)
class Plan:
    harness: Any


@pytest.mark.spec("ARBORA-A2A-R002.2.1")
@pytest.mark.asyncio
async def test_a_subset_is_refused_before_admission_while_the_harness_cannot_enforce_it(
    bare_harness: None,
) -> None:
    assert not carrier_available()
    authority = WorkItemA2AAuthority(
        lambda: pytest.fail("engine was accessed"),
        origins=RunOriginRegistry(),
        config=RuntimeConfig(),
    )
    with pytest.raises(A2AAssemblyUnavailable, match="cannot yet enforce"):
        await authority.dispatch(
            message=_message(),
            idempotency_key="key",
            decision=A2ARouteDecision(
                agent_name="expert",
                selected_tools=("graph_query",),
                selection_mode="agent-assemble",
            ),
        )
    with pytest.raises(A2AAssemblyUnavailable):
        with_tool_subset(Plan(BareHarness()), ("graph_query",))


@pytest.mark.spec("ARBORA-A2A-R002.2.1")
def test_the_plan_carries_the_selected_subset_in_the_harness_field(
    carrying_harness: None,
) -> None:
    assert carrier_available()
    plan = Plan(CarryingHarness(action_space={"keep": 1}))
    carried = with_tool_subset(plan, ("graph_query", "graph_search"))
    assert carried.harness.action_space == {
        "keep": 1,
        "tool_subset": ["graph_query", "graph_search"],
    }
    assert with_tool_subset(plan, ()) is plan  # no subset selected: unrestricted
    assert selected_tools_of(
        {"metadata": {"a2a_route": {"selectedTools": ["graph_query"]}}}
    ) == ("graph_query",)
    assert selected_tools_of({"metadata": {}}) == ()


@pytest.mark.spec("ARBORA-A2A-R002.2.1")
def test_the_native_run_plan_is_planned_with_the_subset_of_its_owner(
    carrying_harness: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        agent_runs, "run_config", lambda config: {"agent_model": "test"}
    )
    import meristem.graph as graph

    monkeypatch.setattr(
        graph,
        "default_run_plan",
        lambda request, config: Plan(CarryingHarness()),
    )
    request = RunRequest(task="hello", run_id="run", metadata={"session": SESSION})
    with scope(tool_subset=("graph_query",)):
        plan = agent_runs.decide_run(request, config=RuntimeConfig())
    assert plan.harness.action_space == {"tool_subset": ["graph_query"]}
    with scope():
        assert (
            agent_runs.decide_run(request, config=RuntimeConfig()).harness.action_space
            is None
        )
