"""Focused graph-os agent, swarm, GUI, and runtime-org execution."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from graph_os.core.handoff import raise_moved
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
    from graph_os.core.event_loop import run_sync_isolated

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
    from graph_os.core.event_loop import run_sync_isolated

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
    raise_moved("_create_reasoning_agent", "core.config", "epistemic-graph")


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
    from agent_utilities.graph.reasoning import (
        COT_SPEC,
        SELF_CONSISTENT_COT_SPEC,
        TOT_BFS_SPEC,
        TOT_DFS_SPEC,
        Budgets,
        record_topology_outcome,
        register_topology,
        run_cot,
        run_self_consistent_cot,
        run_tot,
    )

    budgets = Budgets(loop_budget=max(1, loop_budget))

    if topology in ("tot_bfs", "tot_dfs"):
        agent = _create_reasoning_agent(
            ToTGenerateOutput,
            "You are the expansion step of a Tree-of-Thought search toward "
            "answering a question. Given the question and one current "
            "thought, propose several DISTINCT candidate next thoughts, "
            "each self-scored for promise and self-judged for whether it is "
            "already a complete final answer.",
        )
        generate_fn, score_fn, is_goal_fn = _tot_functions(
            agent, task, branching_factor=max(1, branching_factor)
        )
        spec = TOT_BFS_SPEC if topology == "tot_bfs" else TOT_DFS_SPEC
        strategy: Literal["bfs", "dfs"] = "bfs" if topology == "tot_bfs" else "dfs"
        state, proof = run_tot(
            generate_fn,
            score_fn,
            is_goal_fn,
            question=task,
            budgets=budgets,
            strategy=strategy,
            branching_factor=max(1, branching_factor),
            beam_width=max(1, beam_width),
            max_depth=max(1, max_depth),
        )
        terminal = next((n for n in state.nodes.values() if n.is_terminal), None)
        if terminal is not None:
            answer = terminal.content
        elif state.nodes:
            best = max(state.nodes.values(), key=lambda n: (n.value or 0.0, n.node_id))
            answer = best.content
        else:
            answer = ""
    else:
        agent = _create_reasoning_agent(
            ReasoningStepOutput,
            "You are one step in a graph-of-thought reasoning process "
            "toward answering a question. Given the question and the "
            "reasoning accumulated so far, produce ONLY the next "
            "reasoning step.",
        )
        step_fn = _reasoning_step_fn(agent, task)

        if topology == "self_consistent_cot":
            spec = SELF_CONSISTENT_COT_SPEC
            state, proof, answer = run_self_consistent_cot(
                step_fn,
                question=task,
                num_samples=max(1, num_samples),
                budgets=budgets,
            )
        else:
            spec = COT_SPEC
            state, proof = run_cot(step_fn, question=task, budgets=budgets)
            terminal = next((n for n in state.nodes.values() if n.is_terminal), None)
            answer = terminal.content if terminal is not None else ""

    clean_success = bool(proof.success) and not proof.degraded
    register_topology(engine, spec)
    record_topology_outcome(
        engine,
        spec.topology_id,
        success=proof.success,
        quality_score=0.8 if clean_success else 0.0,
        proof=proof,
    )

    return {
        "topology": spec.name,
        "topology_id": spec.topology_id,
        "answer": answer,
        "node_count": len(state.nodes),
        "rationale_summary": list(state.rationale_summary),
        "termination": proof.as_report(),
    }


async def _run_swarm(
    engine: Any,
    task: str,
    *,
    context: str,
    context_ref: str,
    max_fan_out: int,
) -> dict[str, Any]:
    raise_moved("_run_swarm", "core.config", "epistemic-graph")


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
        raise_moved(
            "graph_agents", "orchestration.computer_use_agent", "epistemic-graph"
        )

    runtime.REGISTERED_TOOLS["graph_agents"] = graph_agents
    runtime.ACTION_TOOL_ROUTES["graph_agents"] = "/graph/agents"

    from graph_os.mcp_server.backing.tools.agent_library_tools import (
        register_agent_library_tool,
    )

    register_agent_library_tool(mcp)
