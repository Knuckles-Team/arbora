"""Trusted A2A native admission binds actual AU scaffold callbacks, not metadata."""

from types import SimpleNamespace

import pytest
from meristem import host_ports
from meristem.harness.plan import PlanState

from arbora import agent_runs
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.mcp_server.task_plan import TaskPlanConfig
from tests.a2a.test_operation_bridge import session as Session
from tests.test_current_plan_publication import claim, engine
from tests.test_current_plan_publication import transport as transport

pytestmark = pytest.mark.spec("ARBORA-OPS-R021.4.5.1.2")


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_only_native_claim_path_binds_actual_au_callback(transport, native):
    session = Session()
    work_engine = engine()
    work_engine.work_item_graph = "__control__"
    seen = []

    class Executor:
        async def execute_agent(self, request, *, session):
            au_request = SimpleNamespace(
                run_id=request.run_id, metadata={"session": session}
            )
            owner = agent_runs.current_native_run(au_request)
            assert owner.claim["fencing_token"] == 7
            async with host_ports.scaffold_publication_scope(
                au_request, PlanState()
            ) as publisher:
                seen.append(publisher is not None)
            return "done"

    authority = WorkItemA2AAuthority(lambda: work_engine, executor=Executor())
    if native:
        authority.task_plan_config = TaskPlanConfig({}, {}, 1, 2)
        authority.hosted_invocation = object()
    result = await authority._execute_claimed_run(
        work_engine,
        item_id="work-item:one",
        session=session,
        claim=claim(),
        agent_name="helper",
        task="inspect",
        task_id="task",
        context_id="context",
        run_id="run",
    )
    assert result == "done"
    assert seen == [native]
    assert len(transport) == int(native)
