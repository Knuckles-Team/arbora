"""The scenario stack and the ``ScenarioRun`` record (ARBORA-ACCEPTANCE-R006).

A scenario enters through arbora's public boundary and leaves one durable
``ScenarioRun`` record in the engine (``MEDULLA-SDLC-R013``). This module holds the
record, the refusal codes a scenario fails with, the phase gate, and the stack the
scenarios run on. The stack adds no second server path: it wraps the host application
the served-operation harness composes and the one engine that application talks to.

The engine's ``ScenarioRun`` record shape is taken from the engine's main branch.
PIN-LAG (remove after the medulla re-pin that carries ``ScenarioRun``): the pinned
release lacks the record, so :func:`record_scenario_run` refuses ``UNAVAILABLE`` naming
the missing shape and writes nothing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCENARIO_STACK_INCOMPLETE = "SCENARIO_STACK_INCOMPLETE"
SCENARIO_ACTION_UNCHANGED = "SCENARIO_ACTION_UNCHANGED"
SCENARIO_CONTROL_MISSING = "SCENARIO_CONTROL_MISSING"
SCENARIO_ACTION_UNCITED = "SCENARIO_ACTION_UNCITED"
DECISION_RECORD_MISSING = "DECISION_RECORD_MISSING"
PHASE_PREREQUISITE_UNVERIFIED = "PHASE_PREREQUISITE_UNVERIFIED"
NO_MEMORY_LIFT = "NO_MEMORY_LIFT"

#: The rows that must be VERIFIED, and the latest P1 run passed, before a later phase runs.
P1_ROWS: tuple[str, ...] = (
    "ARBORA-ACCEPTANCE-R006.1",
    "ARBORA-ACCEPTANCE-R006.1.1",
    "ARBORA-ACCEPTANCE-R006.1.2",
)
#: The components a scenario stack must hold; each is reported by name when absent.
STACK_COMPONENTS: tuple[str, ...] = ("application", "engine", "http", "mcp")


class ScenarioRefused(Exception):
    """A scenario failed with one stable code; ``details`` names what is open."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


class ScenarioCounterfactual(BaseModel):
    """The claim-versus-control actions of one counterfactual run (R006.1.1)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    with_claim_action: str
    without_claim_action: str | None
    refused_by_policy_action: str | None


class ScenarioRun(BaseModel):
    """One scenario verdict, in the engine's ``ScenarioRun`` shape plus its evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scenario_id: str
    phase: str
    result: Literal["passed", "failed"]
    commit: str
    branch: str = "main"
    requirement_ids: tuple[str, ...] = ()
    evidence_digests: tuple[str, ...] = ()
    schema_version: int = Field(default=1, ge=0)
    counterfactual: ScenarioCounterfactual | None = None

    def engine_record(self) -> dict[str, Any]:
        """The ``LifecycleRecordWrite`` body: the engine record, without local evidence."""
        body = self.model_dump(mode="json", exclude={"counterfactual"})
        body["requirement_ids"] = list(self.requirement_ids)
        body["evidence_digests"] = list(self.evidence_digests)
        return {"record_kind": "scenario_run", "record": body}


def check_counterfactual(run: ScenarioCounterfactual) -> None:
    """Refuse a counterfactual whose controls did not run or whose actions are equal."""
    controls = (run.without_claim_action, run.refused_by_policy_action)
    if any(action is None for action in controls):
        raise ScenarioRefused(SCENARIO_CONTROL_MISSING)
    if any(action == run.with_claim_action for action in controls):
        raise ScenarioRefused(SCENARIO_ACTION_UNCHANGED)


@dataclass(frozen=True, slots=True)
class ActionTrace:
    """The trace event of one chosen action (``action_chosen``, R006.1.2)."""

    run_id: str
    action: str
    cited_claim_ids: tuple[str, ...]
    decision_record_id: str | None


def check_provenance(
    trace: ActionTrace,
    retrieved_claim_ids: Iterable[str],
    resolve_decision: Callable[[str], bool],
) -> None:
    """Refuse an action that cites no retrieved claim or whose decision record is absent."""
    retrieved = set(retrieved_claim_ids)
    cited = set(trace.cited_claim_ids)
    if not cited or not cited <= retrieved:
        raise ScenarioRefused(SCENARIO_ACTION_UNCITED)
    if trace.decision_record_id is None or not resolve_decision(
        trace.decision_record_id
    ):
        raise ScenarioRefused(DECISION_RECORD_MISSING)


def require_phase_prerequisite(
    row_states: Mapping[str, str], latest_p1_result: str | None
) -> None:
    """Refuse a later phase until the P1 rows are VERIFIED and the latest P1 run passed."""
    open_rows = [row for row in P1_ROWS if row_states.get(row) != "VERIFIED"]
    if open_rows or latest_p1_result != "passed":
        raise ScenarioRefused(
            PHASE_PREREQUISITE_UNVERIFIED,
            {"phase": "P1", "open_rows": open_rows or ["latest P1 ScenarioRun"]},
        )


def require_memory_lift(
    without_retrieval: Sequence[bool], with_retrieval: Sequence[bool], lift: int = 2
) -> None:
    """P2: the run with retrieval succeeds on at least ``lift`` more tasks."""
    if sum(with_retrieval) - sum(without_retrieval) < lift:
        raise ScenarioRefused(NO_MEMORY_LIFT)


@dataclass(frozen=True)
class ScenarioStack:
    """The served application, its one engine and the callers a scenario uses.

    ``components`` names what the stack holds. A scenario that needs a component the
    stack lacks fails with ``SCENARIO_STACK_INCOMPLETE``; it never skips.
    """

    application: Any
    engine: Any
    http: Callable[..., Any]
    mcp: Callable[..., Any]
    extras: Mapping[str, Any] = field(default_factory=dict)

    def components(self) -> frozenset[str]:
        held = {name for name in STACK_COMPONENTS if getattr(self, name, None)}
        return frozenset(held | set(self.extras))

    def require(self, *needed: str) -> None:
        missing = sorted(set(needed) - self.components())
        if missing:
            raise ScenarioRefused(SCENARIO_STACK_INCOMPLETE, {"missing": missing})


Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]


async def record_scenario_run(send: Send, tenant: str, run: ScenarioRun) -> Any:
    """Write the run as a ``lifecycle_record`` of the engine's ``DecisionLog``.

    PIN-LAG: refuses ``UNAVAILABLE`` naming the shape the pinned release lacks.
    """
    from arbora.pin_lag.medulla import guard

    guard("DecisionLog", models=("ScenarioRun", "LifecycleRecordWriteScenarioRun"))
    params = {
        "op": {
            "op": "lifecycle_record",
            "record": run.engine_record(),
            "tenant_id": tenant,
        }
    }
    return await send("DecisionLog", params, tenant)
