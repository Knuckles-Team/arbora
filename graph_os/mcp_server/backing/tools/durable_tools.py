"""Focused graph-os surface for the unified durable-execution plane (DE2).

One action-routed tool over :mod:`agent_utilities.orchestration.durable_tool_surface`
-- see that module's docstring for exactly which of DE0's five ``CallShape``s are
backed today and why. Matches ``graph_jobs``'s pattern (``mcp/tools/job_tools.py``):
one tool, an ``action`` dispatch, the same engine resolution.
"""

from __future__ import annotations

from typing import Any

from pydantic import Field

from graph_os.core.handoff import raise_moved
from graph_os.mcp_server import runtime


def register_durable_tools(mcp: Any) -> None:
    """Register the unified durable-execution tool surface."""

    @mcp.tool(
        name="graph_durable",
        description=(
            "The unified durable-execution plane (durable-execution-native.md, "
            "supersedes restate natively). Actions: 'run' (start-or-resume a "
            "named DurableRun, returns run_id/resumed/definition_version), "
            "'checkpoint' (DE3: durably record that name already happened, "
            "auto-sequenced -- no hand-picked unique step name needed), "
            "'sleep' (a durable timer bound to one run's one named step -- "
            "fires once wake_at_ms has passed, durably WAITING until then), "
            "'status' (query the :DurableExecutionUnit KG family -- 'what is "
            "durably in flight, waiting on what' across mutation-store/jobs/"
            "statechart/DurableRun). 'state_get'/'state_set'/'call' are named "
            "by DE0's routing contract but NOT backed yet (they route to "
            "eg-statechart/eg-mutation-store, which have no Python-reachable "
            "wire-protocol surface today) -- calling them returns a clear "
            "error naming the gap, never a silent no-op."
        ),
        tags=["graph-os", "durable-execution", "orchestration"],
    )
    async def graph_durable(
        action: str = Field(
            default="status",
            description="run | checkpoint | sleep | status | state_get | state_set | call",
        ),
        session_id: str = Field(
            default="",
            description="Durable run session id (run|checkpoint|sleep|state_get|state_set|call).",
        ),
        name: str = Field(
            default="",
            description="Named step/state key (checkpoint|sleep|state_get|state_set|call).",
        ),
        wake_at_ms: int = Field(
            default=0, description="'sleep' only: epoch-ms deadline to wake at."
        ),
        value: str = Field(
            default="",
            description="'state_set'/'checkpoint' only: JSON value to write/record.",
        ),
        definition_version: str = Field(
            default="",
            description="Optional DE5 version pin for 'run'|'checkpoint'|'sleep'.",
        ),
        kinds: str = Field(
            default="", description="'status' only: JSON list of labels to filter to."
        ),
        status: str = Field(
            default="", description="'status' only: filter by durable_status."
        ),
        limit: int = Field(default=100, description="'status' only: max rows."),
    ) -> str:
        raise_moved(
            "graph_durable", "orchestration.durable_tool_surface", "epistemic-graph"
        )

    # Matches register_job_tools's own pattern (mcp/tools/job_tools.py): an
    # explicit REGISTERED_TOOLS entry (belt-and-suspenders alongside the
    # @mcp.tool() decorator) plus the REST twin, so
    # runtime._mount_rest_routes's generic ACTION_TOOL_ROUTES loop mounts
    # POST /api/graph/durable for free (Two surfaces by default).
    runtime.REGISTERED_TOOLS["graph_durable"] = graph_durable
    runtime.ACTION_TOOL_ROUTES["graph_durable"] = "/graph/durable"
