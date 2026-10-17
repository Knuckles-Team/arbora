"""Focused graph-os surface for durable orchestration jobs."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from graph_os.core.handoff import raise_moved
from graph_os.mcp_server import runtime


def register_job_tools(mcp: Any) -> None:
    """Register durable job dispatch and status operations."""

    @mcp.tool(
        name="graph_jobs",
        description=(
            "Submit and inspect durable orchestration WorkItems. Actions: 'dispatch' "
            "(enqueue a task and return its job handle), 'status' (read a WorkItem, "
            "RunTrace, or workflow-session trace by id), 'cancel' (request "
            "cooperative cancellation through the same WorkItem authority), "
            "'input' (submit a client response to a WorkItem awaiting input -- the "
            "MCP Tasks extension's 'tasks/update' backed by the same WorkItem), "
            "'dead_letter' (list WorkItems whose retry budget is exhausted -- "
            "CONCEPT:AU-KG.ingest.dead-letter-drain, never a silent aggregate-only "
            "count), or 'drain' (explicitly requeue ONE dead-lettered WorkItem as a "
            "fresh attempt -- never automatic, never mutates the original, which "
            "stays visible for audit)."
        ),
        tags=["graph-os", "jobs", "orchestration"],
    )
    async def graph_jobs(
        action: str = Field(
            default="status",
            description="dispatch | status | cancel | input | dead_letter | drain",
        ),
        task: str = Field(default="", description="Task to dispatch."),
        job_id: str = Field(
            default="",
            description=(
                "Job/run/session id for status|cancel|input, or the dead-lettered "
                "WorkItem id for 'drain'."
            ),
        ),
        agent_name: str = Field(
            default="", description="Optional agent hint for the queued turn."
        ),
        dependencies: str = Field(
            default="[]", description="JSON list of prerequisite WorkItem ids."
        ),
        input_responses: str = Field(
            default="{}",
            description=(
                "JSON object of client input responses for action='input' "
                "(the MCP Tasks extension's tasks/update inputResponses)."
            ),
        ),
        kind: str = Field(
            default="", description="'dead_letter' only: filter to one WorkItem kind."
        ),
        limit: int = Field(default=50, description="'dead_letter' only: max rows."),
        reason: str = Field(
            default="", description="'drain' only: why this item is being drained."
        ),
    ) -> str:
        raise_moved("graph_jobs", "orchestration.agent_dispatch", "epistemic-graph")

    runtime.REGISTERED_TOOLS["graph_jobs"] = graph_jobs
    runtime.ACTION_TOOL_ROUTES["graph_jobs"] = "/graph/jobs"
