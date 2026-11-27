"""Development surface reads and reviews (ARBORA-OPS-R896.1, R896.2, R896.5).

``project.mode.get`` and ``project.mode.set`` read and change a repository's
mode, ``development.proposal.list`` and ``development.proposal.review`` list
and decide the learning loop's proposals, and ``spec.query`` answers the spec
artifact graph's competency questions. Each forwards one engine method:
``GetNodeProperties`` of the mode node, ``ProjectModeSet``, ``ProposalList``,
``ProposalDecide`` and ``SpecQuestion``. Arbora keeps no mode, proposal or
answer, never writes under ``specs/`` and never merges.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, model_validator

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import Confirm, OpSpec, PrincipalRule, Verb

READ_SCOPE = "kg:read"
WRITE_SCOPE = "kg:write"

Mode = Literal["reference", "development", "evolving"]
ProposalState = Literal["open", "approved", "rejected", "landed"]
Question = Literal[
    "unreachable_requirements",
    "uncovered_code_items",
    "tasks_blocked_by_contract",
    "principles_at_risk",
    "changes_advancing_goal",
    "next_unblocked_task",
    "unbound_requirements",
    "deployment_of_requirement",
    "open_defects_in_release",
    "changesets_between_deployments",
    "task_context",
    "plan_section",
    "contract",
    "seam_routing",
    "plan_to_goal_phase",
]
#: The questions that take a subject: phase, goal, changeset, requirement or
#: release id, or two deployments.
_SUBJECT = frozenset(
    {
        "principles_at_risk",
        "changes_advancing_goal",
        "next_unblocked_task",
        "deployment_of_requirement",
        "open_defects_in_release",
        "changesets_between_deployments",
        "task_context",
        "plan_section",
        "contract",
        "seam_routing",
        "plan_to_goal_phase",
    }
)
_PROJECT = Field(min_length=1, max_length=256)


class ProjectModeGetParams(Params):
    repository: str = _PROJECT


class ProjectModeSetParams(Params):
    repository: str = _PROJECT
    mode: Mode
    reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def _reason_for_evolving(self) -> ProjectModeSetParams:
        if self.mode == "evolving" and self.reason is None:
            raise ValueError("a move to evolving states its reason")
        return self


class ProjectModeResult(Params):
    repository: str
    mode: Mode
    previous: Mode | None = None


class ProposalListParams(Params):
    repository: str = _PROJECT
    state: ProposalState | None = None
    kind: str | None = Field(default=None, min_length=1, max_length=64)


class ProposalEvidence(Params):
    """One piece of evidence; a gate result carries the engine's sealed verdict or null."""

    kind: str
    record_id: str
    digest: str
    gate_verdict: Any | None = None


class ProposalEntry(Params):
    id: str
    kind: str
    target: str
    evidence: list[ProposalEvidence]
    state: ProposalState


class ProposalListResult(Params):
    items: list[ProposalEntry]


class PullRequestBranches(Params):
    """The two branches of the pull request an acceptance opens; the engine resolves the rest."""

    head: str = Field(min_length=1, max_length=256)
    base: str = Field(min_length=1, max_length=256)


class ProposalReviewParams(Params):
    proposal_id: str = Field(min_length=1, max_length=256)
    decision: Literal["accept", "reject"]
    reason: str = Field(min_length=1, max_length=500)
    pull_request: PullRequestBranches | None = None


class ProposalReviewResult(Params):
    id: str
    state: ProposalState
    pull_request: str | None = None


class SpecQueryParams(Params):
    question: Question
    subject: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def _subject_matches_question(self) -> SpecQueryParams:
        if (self.question in _SUBJECT) != (self.subject is not None):
            raise ValueError("a subject is given exactly when the question takes one")
        return self


class SpecQueryResult(Params):
    rows: list[dict[str, str]]
    digests: list[str]


async def handle_project_mode_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One repository's mode as the engine reads it; no record answers ``reference``."""
    record = await engine_send(
        context, "ProjectModeGet", {"request": {"project": params["repository"]}}
    )
    mode = record.get("mode") if isinstance(record, Mapping) else None
    if mode not in ("reference", "development", "evolving"):
        raise unavailable("engine result malformed")
    return {"repository": params["repository"], "mode": mode}


async def handle_project_mode_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Change the mode. The confirmed plan is the operator approval, held by that person."""
    request: dict[str, Any] = {
        "project": params["repository"],
        "mode": params["mode"],
    }
    if params["mode"] == "evolving":
        request["approval"] = {
            "approver": str(context.caller.principal),
            "approved_at_ms": int(time.time() * 1000),
            "reason": params["reason"],
        }
    receipt = await engine_send(
        context, "ProjectModeSet", {"request": request}, idempotent=True
    )
    try:
        return {
            "repository": params["repository"],
            "mode": receipt["mode"],
            "previous": receipt["previous"],
        }
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


async def handle_proposal_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The proposals of one repository, as the engine lists them for review."""
    request: dict[str, Any] = {"repo": params["repository"]}
    for key in ("state", "kind"):
        if params.get(key) is not None:
            request[key] = params[key]
    rows = await engine_send(context, "ProposalList", {"request": request})
    if isinstance(rows, Mapping):
        rows = rows.get("proposals", rows.get("entries"))
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    items = []
    verdicts: dict[str, Any] = {}
    try:
        for row in rows:
            evidence = []
            for e in row["evidence"]:
                verdict = None
                if e["kind"] == "gate_result":
                    if e["record_id"] not in verdicts:
                        verdicts[e["record_id"]] = await _sealed_verdict(
                            context, e["record_id"]
                        )
                    verdict = verdicts[e["record_id"]]
                evidence.append(
                    {
                        "kind": e["kind"],
                        "record_id": e["record_id"],
                        "digest": e["digest"],
                        "gate_verdict": verdict,
                    }
                )
            items.append(
                {
                    "id": row["id"],
                    "kind": row["kind"],
                    "target": row["target"],
                    "evidence": evidence,
                    "state": row["state"],
                }
            )
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None
    return {"items": items}


async def _sealed_verdict(context: Any, record_id: str) -> Any | None:
    """The verdict of the engine's gate record, or ``None`` when it holds none.

    The gate record is sealed and create-only (MEDULLA-SCM-R008), so what the
    engine returns is what the gate recorded; Arbora adds and infers nothing.
    """
    try:
        record = await engine_send(context, "GetNodeProperties", {"node_id": record_id})
    except EngineRefusal:
        return None
    if not isinstance(record, Mapping):
        return None
    return record.get("verdict")


async def handle_proposal_review(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Record an accept or reject by the verified reviewer; the engine opens the pull request."""
    request: dict[str, Any] = {
        "id": params["proposal_id"],
        "decision": "approve" if params["decision"] == "accept" else "reject",
        "reviewer": str(context.caller.principal),
        "reason": params["reason"],
    }
    if params.get("pull_request") is not None:
        request["pull_request"] = dict(params["pull_request"])
    receipt = await engine_send(
        context, "ProposalDecide", {"request": request}, idempotent=True
    )
    try:
        return {
            "id": receipt["id"],
            "state": receipt["state"],
            "pull_request": receipt.get("pull_request"),
        }
    except (KeyError, TypeError, AttributeError):
        raise unavailable("engine result malformed") from None


async def handle_spec_query(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One competency question over the caller's spec artifact graph."""
    graph = await caller_dispatch_graph(context)
    answer = await engine_send(
        context,
        "SpecQuestion",
        {
            "question": params["question"],
            "subject": params.get("subject"),
            "graph": graph,
        },
    )
    try:
        return {"rows": list(answer["rows"]), "digests": list(answer["digests"])}
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


NATIVE_IDEMPOTENCY: dict[str, str] = {
    "project.mode.set": "medulla:ProjectModeSet",
    "development.proposal.review": "medulla:ProposalDecide",
}


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.development"
    return (
        build_read_op(
            op_id="project.mode.get",
            summary="Read a repository's mode: reference, development or evolving",
            examples=("what mode is this repository in",),
            params=ProjectModeGetParams,
            result=ProjectModeResult,
            handler=f"{module}.handle_project_mode_get",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="project.mode.set",
            summary="Change a repository's mode; only an undelegated person may, and that person is the approver",
            examples=("set this repository to development mode",),
            params=ProjectModeSetParams,
            result=ProjectModeResult,
            handler=f"{module}.handle_project_mode_set",
            scope=WRITE_SCOPE,
            verb=Verb.MANAGE,
            confirm=Confirm.PLAN,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
        ),
        build_read_op(
            op_id="development.proposal.list",
            summary="List a repository's proposals with kind, target, evidence and state",
            examples=("list the open proposals for this repository",),
            params=ProposalListParams,
            result=ProposalListResult,
            handler=f"{module}.handle_proposal_list",
            scope=READ_SCOPE,
            verb=Verb.FIND,
        ),
        build_write_op(
            op_id="development.proposal.review",
            summary="Accept or reject one open proposal as the verified reviewer",
            examples=("accept this proposal",),
            params=ProposalReviewParams,
            result=ProposalReviewResult,
            handler=f"{module}.handle_proposal_review",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="spec.query",
            summary="Answer a competency question over the spec artifact graph",
            examples=("what is the next unblocked task of this phase",),
            params=SpecQueryParams,
            result=SpecQueryResult,
            handler=f"{module}.handle_spec_query",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_project_mode_get",
    "handle_project_mode_set",
    "handle_proposal_list",
    "handle_proposal_review",
    "handle_spec_query",
    "operations",
    "specs",
]
