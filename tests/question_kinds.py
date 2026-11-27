"""A structural double of an engine release that names ``Decide`` question kinds."""

from __future__ import annotations

import typing

import pytest


def declare_question_kinds(monkeypatch: pytest.MonkeyPatch, *kinds: str) -> None:
    """Make the pinned contract name each ``kind`` (snake_case) as a ``QuestionKind``."""
    from medulla.generated import models

    literal = typing.Literal[kinds]  # type: ignore[valid-type]
    monkeypatch.setattr(models, "QuestionKind", models.QuestionKind | literal)
    declare_autonomy_shapes(monkeypatch)


def declare_autonomy_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structural double of the typed autonomy shapes of engine ``main`` (R867).

    The shapes mirror ``medulla/generated/models.py`` on ``origin/main``: the request
    body ``AutonomyRequest``, its ``IrreversibilityClass`` and the typed answer
    ``FamilyAnswerAutonomy`` carrying an ``AutonomyReading``.
    """
    import enum

    from medulla.generated import models
    from pydantic import BaseModel, ConfigDict

    class IrreversibilityClass(enum.StrEnum):
        REVERSIBLE = "reversible"
        IRREVERSIBLE = "irreversible"

    class CorrigibilityVerdict(enum.StrEnum):
        ALLOW = "allow"
        REQUIRE_CONFIRMATION = "require_confirmation"

    class AutonomyRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        irreversibility: IrreversibilityClass

    class AutonomyReading(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        verdict: CorrigibilityVerdict
        clean_outcomes: int
        code: str | None = None

    class FamilyAnswerAutonomy(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        type: typing.Literal["autonomy"]
        value: AutonomyReading

    for name, value in (
        ("IrreversibilityClass", IrreversibilityClass),
        ("AutonomyRequest", AutonomyRequest),
        ("FamilyAnswerAutonomy", FamilyAnswerAutonomy),
    ):
        monkeypatch.setattr(models, name, value, raising=False)


#: Every engine ``QuestionKind`` wire value on medulla main (snake_case), for the wire test.
ENGINE_QUESTION_KINDS: frozenset[str] = frozenset(
    {
        "pre_tool_risk",
        "connector_event_triage",
        "connector_tool_choice",
        "connector_write_back",
        "tool_subset",
        "harness_config",
        "task_plan",
        "harness_contrast",
        "placement",
        "context_budget",
        "round_verdict",
        "stage_allocation",
        "probe_verdict",
        "item_recommendation",
        "load_set",
        "usage_guidance",
        "branch_outcome",
        "edit_concentration",
        "recruit",
        "resolve_conflict",
        "council_coordination",
        "merge_arming",
        "revert_or_fix_forward",
        "defect_triage",
        "promotion",
        "reasoning_paradigm",
        "model_capacity",
        "route",
        "rank",
        "classify",
        "resolve_entity",
        "schema_mapping",
        "template_choice",
        "retrieval_plan",
        "ingestion_lane",
        "enrichment_schedule",
        "placement_plan",
        "over_window_plan",
        "instruction_disclosure",
        "changeset_review",
        "temporal_basis",
        "thought_search",
        "comparative_analysis_plan",
        "context_package",
        "topology",
        "program_lookup",
        "skill_workflow_compile",
        "skill_upgrade",
        "team_coordination",
        "capability_designation",
        "context_replacement",
        "recursion_mode",
        "sub_call_admission",
        "command_config",
        "run_adjustment",
        "path_preference",
        "correction_admission",
        "field_shielding",
        "graph_intake",
        "tool_necessity",
        "decision_table_eval",
        "branch_select",
        "feedback_loop",
        "planning_admission",
        "tool_action_admission",
        "a2a_route",
        "paper_admission",
        "held_item_admission",
        "criterion_review",
        "generated_code_admission",
        "credential_reuse",
        "runtime_quota",
        "insight_selection",
        "tool_relevance",
        "fanout_aggregate",
        "delegation_admission",
        "answer_ranking",
        "serving_technique_activation",
        "process_remediation",
        "ingest_shortfall",
        "capacity_proposal",
        "probe_selection",
        "reward_decomposition",
        "research_next",
        "feature_match",
        "curriculum_promotion",
        "population_drift",
        "fanout_width",
        "autonomy_verdict",
        "trajectory_join",
        "step_verification",
        "adversarial_admission",
        "skill_evolution",
        "playbook_triage",
        "trajectory_ladder",
        "analyst_synthesis",
        "process_plan",
    }
)
