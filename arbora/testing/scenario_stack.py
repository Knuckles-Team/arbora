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
CLAIM_LINEAGE_MISSING = "CLAIM_LINEAGE_MISSING"
WRITE_CONFLICT_MISSING = "WRITE_CONFLICT_MISSING"
WRITE_LOST = "WRITE_LOST"
RESTRICTED_NODE_VISIBLE = "RESTRICTED_NODE_VISIBLE"
CONTRADICTION_UNRECORDED = "CONTRADICTION_UNRECORDED"
PLAN_GATE_FAILED = "PLAN_GATE_FAILED"
ONE_PLAN_VIOLATED = "ONE_PLAN_VIOLATED"
FACT_PROVENANCE_MISSING = "FACT_PROVENANCE_MISSING"
UNREADABLE_SOURCE_ANSWERED = "UNREADABLE_SOURCE_ANSWERED"
COVERAGE_BLOCK_MISSING = "COVERAGE_BLOCK_MISSING"
CHANNEL_ANSWER_DIFFERS = "CHANNEL_ANSWER_DIFFERS"

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
    from arbora.api.ops.pin_guards import guard

    guard("DecisionLog", models=("ScenarioRun", "LifecycleRecordWriteScenarioRun"))
    params = {
        "op": {
            "op": "lifecycle_record",
            "record": run.engine_record(),
            "tenant_id": tenant,
        }
    }
    return await send("DecisionLog", params, tenant)


#: What every claim of a scenario write carries (R006.2 lineage, R006.3 provenance).
CLAIM_LINEAGE_FIELDS: tuple[str, ...] = ("source", "actor", "recorded_at", "confidence")


def check_claim_lineage(claims: Iterable[Mapping[str, Any]]) -> int:
    """Refuse a claim that lacks source, actor, time or confidence; return the count."""
    count = 0
    for claim in claims:
        count += 1
        missing = [f for f in CLAIM_LINEAGE_FIELDS if claim.get(f) in (None, "")]
        if missing:
            raise ScenarioRefused(
                CLAIM_LINEAGE_MISSING, {"claim": claim.get("id"), "missing": missing}
            )
    return count


def filter_claims(
    claims: Iterable[Mapping[str, Any]],
    *,
    source: str | None = None,
    min_confidence: float = 0.0,
    valid_at: int | None = None,
) -> list[Mapping[str, Any]]:
    """The claims of ``source`` at or above ``min_confidence`` and valid at ``valid_at``."""
    kept: list[Mapping[str, Any]] = []
    for claim in claims:
        if source is not None and claim.get("source") != source:
            continue
        if float(claim.get("confidence", 0.0)) < min_confidence:
            continue
        if valid_at is not None and not (
            claim.get("valid_from", 0) <= valid_at < claim.get("valid_to", 2**62)
        ):
            continue
        kept.append(claim)
    return kept


def check_write_conflict(first: Mapping[str, Any], second: Mapping[str, Any]) -> None:
    """Two writers at one version: the first commits, the second loses the compare-and-set.

    The served memory write answers a lost compare-and-set as ``swapped: false`` with
    the current version (``ARBORA-OPS-R844``); that is the one accepted form. A typed
    ``WRITE_CONFLICT`` error body is a different surface and does not satisfy this check.
    """
    if first.get("swapped") is not True or second.get("swapped") is not False:
        raise ScenarioRefused(
            WRITE_CONFLICT_MISSING, {"first": dict(first), "second": dict(second)}
        )


def check_no_write_lost(written: Iterable[str], stored: Iterable[str]) -> None:
    """Every acknowledged write is stored."""
    lost = sorted(set(written) - set(stored))
    if lost:
        raise ScenarioRefused(WRITE_LOST, {"lost": lost})


def check_restricted_hidden(
    visible_ids: Iterable[str], restricted_ids: Iterable[str]
) -> None:
    """The restricted principal sees zero restricted nodes (R006.3 step 3)."""
    leaked = sorted(set(visible_ids) & set(restricted_ids))
    if leaked:
        raise ScenarioRefused(RESTRICTED_NODE_VISIBLE, {"count": len(leaked)})


def check_contradiction(
    record: Mapping[str, Any] | None,
    claim_ids: tuple[str, str],
    kept_ids: Iterable[str],
) -> None:
    """The record names both claims and both claims stay (R006.3 step 4)."""
    named = {record.get("left"), record.get("right")} if record else set()
    if named != set(claim_ids) or not set(claim_ids) <= set(kept_ids):
        raise ScenarioRefused(CONTRADICTION_UNRECORDED, {"claims": list(claim_ids)})


def check_one_plan(plan: Mapping[str, Any]) -> None:
    """One plan, one transaction, one security context joins the three modalities (R006.4)."""
    stages = {stage.get("modality") for stage in plan.get("stages", ())}
    one = (
        plan.get("transaction")
        and plan.get("security_context")
        and len(plan.get("plans", ())) == 1
    )
    if not {"graph", "vector", "attribute"} <= stages or not one:
        raise ScenarioRefused(ONE_PLAN_VIOLATED, {"stages": sorted(map(str, stages))})


def check_plan_gate(verdict: Mapping[str, Any] | None) -> None:
    """The falsification gate verdict is recorded and passed (R006.4 step 1)."""
    if not verdict or verdict.get("passed") is not True:
        raise ScenarioRefused(PLAN_GATE_FAILED)


def check_fact_provenance(facts: Iterable[Mapping[str, Any]]) -> int:
    """Each fact names its source, its mapping and its read time (R006.7 step 1)."""
    count = 0
    for fact in facts:
        count += 1
        if not all(fact.get(f) for f in ("source", "mapping", "read_at")):
            raise ScenarioRefused(FACT_PROVENANCE_MISSING, {"fact": fact.get("id")})
    return count


def check_coverage(answer: Mapping[str, Any], unreadable: Iterable[str]) -> None:
    """No fact of an unreadable source; each excluded source named with a stable reason."""
    excluded = set(unreadable)
    cited = {fact.get("source") for fact in answer.get("facts", ())}
    if cited & excluded:
        raise ScenarioRefused(
            UNREADABLE_SOURCE_ANSWERED, {"sources": sorted(cited & excluded)}
        )
    block = {
        item.get("source"): item.get("reason")
        for item in answer.get("coverage", {}).get("excluded", ())
    }
    if set(block) != excluded or not all(block.values()):
        raise ScenarioRefused(COVERAGE_BLOCK_MISSING, {"sources": sorted(excluded)})


def check_same_answer(web: Mapping[str, Any], channel: Mapping[str, Any]) -> None:
    """The browser route and the channel return the same text, citations and coverage."""
    for field_name in ("text", "citations", "coverage"):
        if web.get(field_name) != channel.get(field_name) or field_name not in web:
            raise ScenarioRefused(CHANNEL_ANSWER_DIFFERS, {"field": field_name})
