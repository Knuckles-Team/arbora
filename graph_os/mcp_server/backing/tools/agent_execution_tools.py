"""Focused graph-os agent, swarm, GUI, and runtime-org execution."""

from __future__ import annotations

import json
import logging
from typing import Any

from agent_utilities.security.error_surface import public_error_text
from pydantic import BaseModel, Field

from graph_os.api.errors import not_served
from graph_os.mcp_server import runtime

logger = logging.getLogger(__name__)


class ReasoningStepOutput(BaseModel):
    """One LLM-produced step of a :mod:`agent_utilities.graph.reasoning` run.

    ``summary`` is a short one-line SUMMARY of this step's reasoning (never
    the raw private chain-of-thought — matches the truthfulness contract
    :meth:`~agent_utilities.graph.reasoning.state.ReasoningState.
    append_rationale` documents). ``content`` is the next intermediate
    thought, or — when ``is_final`` — the final answer text.
    """

    summary: str
    content: str
    is_final: bool


def _reasoning_step_fn(agent: Any, task: str):
    """Build a CoT/self-consistent-CoT ``StepFn`` driven by a real LLM agent.

    Shared by both topologies :func:`_run_reasoning_topology` supports: each
    call renders the running :class:`~agent_utilities.graph.reasoning.state.
    ReasoningState` (its accumulated rationale summaries + the deepest node's
    content so far) into a prompt, runs the agent for exactly one step, and
    returns the ``(summary, content, is_final)`` tuple
    :func:`agent_utilities.graph.reasoning.cot.run_cot`/``run_self_consistent_cot``
    require. ``agent is None`` (no reachable model) degrades to an immediate,
    honest single-step termination rather than looping forever with no model.
    """
    from agent_utilities.core.event_loop import run_sync_isolated

    def step_fn(state: Any) -> tuple[str, str, bool]:
        if agent is None:
            return "no reachable model", task, True
        history = "\n".join(f"- {s}" for s in state.rationale_summary[-8:])
        prior_content = ""
        if state.nodes:
            prior_content = max(state.nodes.values(), key=lambda n: n.depth).content
        prompt = (
            f"QUESTION:\n{task}\n\n"
            f"REASONING SO FAR ({len(state.nodes)} node(s)):\n"
            f"{history or '(none yet)'}\n\n"
            f"MOST RECENT CONTENT:\n{prior_content or '(none — this is the first step)'}\n\n"
            "Produce the NEXT reasoning step. Set is_final=true only once "
            "'content' IS the final answer."
        )
        try:
            result = run_sync_isolated(lambda: agent.run_sync(prompt))
            step: ReasoningStepOutput = result.output
            return step.summary, step.content, bool(step.is_final)
        except Exception as exc:  # noqa: BLE001 — a failed step ends the chain honestly
            return (
                f"reasoning step failed: {type(exc).__name__}",
                prior_content or task,
                True,
            )

    return step_fn


class ToTChildOutput(BaseModel):
    """One candidate next-thought a ToT ``generate_fn`` call proposes.

    ``score``/``is_goal`` are produced in the SAME call that proposes
    ``content`` — never a separate round-trip — so :func:`_tot_functions`'s
    ``score_fn``/``is_goal_fn`` can answer from cache instead of re-querying
    the model once per child (bounds ToT's LLM-call count to one call per
    EXPANDED node, matching :func:`_reasoning_step_fn`'s one-call-per-step
    contract for CoT).
    """

    content: str
    score: float = Field(ge=0.0, le=1.0)
    is_goal: bool


class ToTGenerateOutput(BaseModel):
    """The full structured output one ToT expansion call produces."""

    children: list[ToTChildOutput]


def _tot_functions(
    agent: Any, task: str, *, branching_factor: int
) -> tuple[Any, Any, Any]:
    """Build LLM-backed ``(generate_fn, score_fn, is_goal_fn)`` for :func:`~agent_utilities.graph.reasoning.tot.run_tot`.

    ``agent is None`` (no reachable model) degrades every function to an
    honest, immediate empty/zero/false answer — :func:`run_tot` then
    converges on ``degraded=True`` rather than looping forever with no model,
    mirroring :func:`_reasoning_step_fn`'s same no-model contract.
    """
    from agent_utilities.core.event_loop import run_sync_isolated

    # content -> (score, is_goal), populated by generate_fn so score_fn/
    # is_goal_fn (called separately, per child, by run_tot's own loop) never
    # re-query the model for something this SAME expansion call already rated.
    cache: dict[str, tuple[float, bool]] = {}

    def generate_fn(parent_content: str) -> list[str]:
        if agent is None:
            return []
        prompt = (
            f"QUESTION:\n{task}\n\n"
            f"CURRENT THOUGHT:\n{parent_content}\n\n"
            f"Propose up to {branching_factor} DISTINCT next reasoning steps "
            "that could each extend this thought toward answering the "
            "question. For EACH one, self-rate its promise (0.0-1.0) and "
            "whether it already IS a complete, final answer (is_goal)."
        )
        try:
            result = run_sync_isolated(lambda: agent.run_sync(prompt))
            output: ToTGenerateOutput = result.output
        except Exception as exc:  # noqa: BLE001 — a failed expansion ends that branch honestly
            logger.debug("tot: generate_fn step failed: %s", type(exc).__name__)
            return []
        children = list(output.children)[:branching_factor]
        for child in children:
            cache[child.content] = (child.score, child.is_goal)
        return [child.content for child in children]

    def score_fn(content: str) -> float:
        return cache.get(content, (0.0, False))[0]

    def is_goal_fn(content: str) -> bool:
        # Uncached only for the ROOT question itself (run_tot's very first
        # is_goal_fn call, before any generate_fn call has populated the
        # cache) -- a bare question is never itself a complete answer, so
        # False is the honest default, not a guess standing in for a model call.
        return cache.get(content, (0.0, False))[1]

    return generate_fn, score_fn, is_goal_fn


def _create_reasoning_agent(output_type: type, system_prompt: str) -> Any:
    """Build the live LLM agent one reasoning topology's step/expansion
    function drives — the configured chat model (``DEFAULT_LLM_PROVIDER``/
    ``DEFAULT_KG_MODEL_ID``, the fleet's vLLM-served model), or ``None`` on
    any construction failure (no reachable model) so every topology's own
    functions can degrade honestly rather than raise.
    """
    from agent_utilities.core.config import DEFAULT_KG_MODEL_ID, DEFAULT_LLM_PROVIDER
    from agent_utilities.core.contextual_model import create_context_agent
    from agent_utilities.core.model_factory import create_model

    try:
        model = create_model(
            provider=DEFAULT_LLM_PROVIDER, model_id=DEFAULT_KG_MODEL_ID
        )
    except Exception:
        return None
    try:
        return create_context_agent(
            model=model, output_type=output_type, system_prompt=system_prompt
        )
    except Exception:
        return None


async def _run_reasoning_topology(
    engine: Any,
    *,
    topology: str,
    task: str,
    num_samples: int,
    loop_budget: int,
    branching_factor: int = 3,
    beam_width: int = 3,
    max_depth: int = 5,
) -> dict[str, Any]:
    """Run a real :mod:`agent_utilities.graph.reasoning` topology over ``task``.

    CONCEPT:AU-ORCH.planning.reasoning-graph-topologies — the live entry point
    the reasoning-topology package lacked (built + unit-tested, zero live
    callers). Drives the real ``run_cot``/``run_self_consistent_cot``/``run_tot``
    entry points with an LLM-backed step/expansion function
    (:func:`_reasoning_step_fn`/:func:`_tot_functions`), then registers/updates
    the run as a versioned, graph-addressable topology resource through
    :mod:`agent_utilities.graph.reasoning.topology`'s own API
    (:func:`~.topology.register_topology`, :func:`~.topology.
    record_topology_outcome`) — the SAME best-effort, engine-optional pattern
    :class:`agent_utilities.graph.topology_engine.TopologyEngine` already uses
    for team-composition topologies, reused here rather than reimplemented.

    ``topology in {"tot_bfs", "tot_dfs"}`` runs Tree-of-Thought (D-5.3-5.6-3):
    unlike CoT's single linear chain, each expanded node fans out to up to
    ``branching_factor`` candidate next-thoughts in ONE LLM call
    (:class:`ToTGenerateOutput`), each self-scored and self-judged for
    goal-completion by the SAME call — never a separate round-trip per child.
    """
    raise not_served("graph.reasoning", "EG-DECISION-ENGINE-R750")


async def _run_swarm(
    engine: Any,
    task: str,
    *,
    context: str,
    context_ref: str,
    max_fan_out: int,
) -> dict[str, Any]:
    raise not_served(
        "graph.parallel_engine", "AU-harness (deleted from AU)", owner="agent-utilities"
    )


def register_agent_execution_tools(mcp: Any) -> None:
    """Register focused multi-agent execution capabilities."""

    @mcp.tool(
        name="graph_agents",
        description=(
            "Execute graph-grounded agent collectives. Actions: 'swarm' performs governed "
            "goal decomposition and parallel-wave synthesis; 'computer_use' drives a GUI "
            "sandbox; 'synthesize_org' builds a staffed runtime org; 'run_org' executes its "
            "WorkItem DAG; 'reason' runs a versioned graph/reasoning topology (CoT, "
            "self-consistent CoT, or Tree-of-Thought BFS/DFS — "
            "CONCEPT:AU-ORCH.planning.reasoning-graph-topologies) over 'task' via a real "
            "LLM step/expansion function, then registers/updates the run as a "
            "graph-addressable topology resource. Single-agent delegation remains "
            "graph_orchestrate."
        ),
        tags=["graph-os", "agents", "swarm", "computer-use", "org", "reasoning"],
    )
    async def graph_agents(
        action: str = Field(
            default="swarm",
            description="swarm | computer_use | synthesize_org | run_org | reason",
        ),
        task: str = Field(default="", description="Goal or GUI task."),
        context: str = Field(default="", description="Curated swarm context."),
        context_ref: str = Field(
            default="", description="Persisted ContextBlob id for the swarm."
        ),
        max_fan_out: int = Field(default=5, ge=1),
        max_steps: int = Field(default=30, ge=1),
        host: str = Field(default="", description="Computer-use inventory host alias."),
        container_id: str = Field(
            default="", description="Existing GUI sandbox container id."
        ),
        options_json: str = Field(
            default="{}",
            description="JSON options; org actions accept {domains:[...]}.",
        ),
        topology: str = Field(
            default="cot",
            description=(
                "reason only: 'cot' | 'self_consistent_cot' | 'tot_bfs' | 'tot_dfs'."
            ),
        ),
        num_samples: int = Field(
            default=3,
            ge=1,
            description="reason only: self_consistent_cot sample count.",
        ),
        branching_factor: int = Field(
            default=3,
            ge=1,
            description="reason(tot_bfs|tot_dfs) only: candidate next-thoughts per expansion.",
        ),
        beam_width: int = Field(
            default=3,
            ge=1,
            description="reason(tot_bfs|tot_dfs) only: frontier survivors kept per expansion.",
        ),
        max_depth: int = Field(
            default=5,
            ge=1,
            description="reason(tot_bfs|tot_dfs) only: max thought-chain depth before giving up.",
        ),
    ) -> str:
        engine = runtime._get_engine()
        if engine is None:
            return "Error: IntelligenceGraphEngine not active."
        try:
            if action == "swarm":
                if not task:
                    raise ValueError("task is required for swarm")
                return json.dumps(
                    await _run_swarm(
                        engine,
                        task,
                        context=context,
                        context_ref=context_ref,
                        max_fan_out=max_fan_out,
                    ),
                    default=str,
                )

            if action == "reason":
                if not task:
                    raise ValueError("task is required for reason")
                topology_norm = (topology or "cot").strip().lower()
                if topology_norm not in {
                    "cot",
                    "self_consistent_cot",
                    "tot_bfs",
                    "tot_dfs",
                }:
                    raise ValueError(
                        f"unknown topology {topology_norm!r} for reason; choose "
                        "'cot', 'self_consistent_cot', 'tot_bfs', or 'tot_dfs'"
                    )
                return json.dumps(
                    await _run_reasoning_topology(
                        engine,
                        topology=topology_norm,
                        task=task,
                        num_samples=num_samples,
                        branching_factor=branching_factor,
                        beam_width=beam_width,
                        max_depth=max_depth,
                        loop_budget=max_steps,
                    ),
                    default=str,
                )

            if action == "computer_use":
                from agent_utilities.orchestration.computer_use_agent import (
                    provision_and_run_computer_use,
                    run_computer_use_task,
                )

                if container_id:
                    return await run_computer_use_task(
                        task, container_id, host=host or None, engine=engine
                    )
                return await provision_and_run_computer_use(
                    task, host=host or None, engine=engine
                )

            options = json.loads(options_json) if options_json else {}
            if not isinstance(options, dict):
                raise ValueError("options_json must decode to an object")
            domains = options.get("domains")
            if action == "synthesize_org":
                from agent_utilities.orchestration.org_runtime import Recruiter

                if not task:
                    raise ValueError("task is required for synthesize_org")
                return json.dumps(
                    Recruiter(engine).synthesize_org(task, domains=domains).to_dict(),
                    default=str,
                )
            if action == "run_org":
                from agent_utilities.orchestration.org_runtime import OrgRuntime

                if not task:
                    raise ValueError("task is required for run_org")
                return json.dumps(
                    await OrgRuntime(engine, max_steps=max_steps).run(
                        task, domains=domains
                    ),
                    default=str,
                )
            return f"Error: Unknown graph_agents action '{action}'"
        except PermissionError:
            raise
        except Exception as exc:
            return public_error_text(exc)

    runtime.REGISTERED_TOOLS["graph_agents"] = graph_agents
    runtime.ACTION_TOOL_ROUTES["graph_agents"] = "/graph/agents"

    from graph_os.mcp_server.backing.tools.agent_library_tools import (
        register_agent_library_tool,
    )

    register_agent_library_tool(mcp)
