"""ARBORA-OPS-R012 / ARBORA-FLEET-R014: bounded resolver for NL operation lookup.

Covers combined lexical/semantic ranking order, the bounded result cache,
the read-only preview fallback for an unmatched ``ask`` call (never a direct
mutation), and (ARBORA-FLEET-R014) that ``record_execution``'s outcome
feedback is an EMA partitioned by the caller's opaque ``scope_ref`` so one
tenant/policy partition's learned reward never leaks into another's rank.
"""

from __future__ import annotations

import pytest

from arbora.api.mcp.resolve import Descriptor, IntentResolver

_DESCRIPTORS = (
    Descriptor(
        id="deploy.restart",
        verb="act",
        summary="Restart a deployed service",
        examples=("restart the service",),
        domain="deploy",
        tags=("deploy", "restart"),
        params_schema={"required": ["service"]},
    ),
    Descriptor(
        id="deploy.status",
        verb="act",
        summary="Check whether a deployment finished",
        examples=("is the deploy done",),
        domain="deploy",
        tags=("deploy", "status"),
        params_schema={},
    ),
)

_NL_DESCRIPTOR = Descriptor(
    id="query.nl", verb="ask", summary="Plan a natural-language query"
)


@pytest.mark.spec("ARBORA-OPS-R012")
def test_rank_orders_lexical_name_match_above_description_only_match() -> None:
    resolver = IntentResolver()
    ranked = resolver.rank("act", "restart the deploy", _DESCRIPTORS, scope_ref=None)

    assert [candidate.descriptor.id for candidate in ranked] == [
        "deploy.restart",
        "deploy.status",
    ]
    assert ranked[0].score > ranked[1].score


@pytest.mark.spec("ARBORA-OPS-R012")
def test_rank_is_cached_for_an_identical_visible_snapshot() -> None:
    resolver = IntentResolver()
    first = resolver.rank("act", "restart the deploy", _DESCRIPTORS, scope_ref=None)
    second = resolver.rank("act", "restart the deploy", _DESCRIPTORS, scope_ref=None)

    assert first is second
    assert len(resolver._cache) == 1


@pytest.mark.spec("ARBORA-OPS-R012")
def test_cache_evicts_oldest_entry_once_over_its_bound() -> None:
    resolver = IntentResolver(cache_limit=2)
    for n in range(3):
        resolver.rank("act", f"restart deploy number {n}", _DESCRIPTORS, scope_ref=None)

    assert len(resolver._cache) == 2


@pytest.mark.spec("ARBORA-OPS-R012")
def test_unmatched_ask_falls_back_to_read_only_nl_preview() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "ask",
        "why did the deploy fail",
        (*_DESCRIPTORS, _NL_DESCRIPTOR),
        scope_ref=None,
    )

    assert resolution.fallback is True
    assert resolution.op == "query.nl"
    assert resolution.params == {"text": "why did the deploy fail"}
    assert resolution.preview is False, (
        "ask fallback must never require a mutation plan"
    )


@pytest.mark.spec("ARBORA-OPS-R012")
def test_unmatched_ask_without_nl_refuses_rather_than_mutating() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "ask", "why did the deploy fail", _DESCRIPTORS, scope_ref=None
    )

    assert resolution.op is None
    assert resolution.fallback is False
    assert resolution.preview is False


@pytest.mark.spec("ARBORA-OPS-R012")
def test_matched_mutation_verb_always_requires_a_reviewed_preview() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "act", "restart the deploy", _DESCRIPTORS, scope_ref=None
    )

    assert resolution.op == "deploy.restart"
    assert resolution.preview is True


@pytest.mark.spec("ARBORA-FLEET-R014")
def test_outcome_feedback_is_partitioned_by_scope_ref() -> None:
    """A recorded outcome under one tenant/policy scope never leaks into another's rank.

    ``record_execution`` folds an EMA of observed success into the next
    ``rank`` call for the *same* ``scope_ref``, but a different tenant or
    policy revision (a different opaque ``scope_ref``) must see the
    unbiased baseline score, not the first partition's learned reward.
    """

    resolver = IntentResolver()
    scope_a = resolver.scope_ref("tenant-a", "policy-1")
    scope_b = resolver.scope_ref("tenant-b", "policy-1")

    baseline = resolver.rank(
        "act", "restart the deploy", _DESCRIPTORS, scope_ref=scope_a
    )
    baseline_score = next(
        c.score for c in baseline if c.descriptor.id == "deploy.restart"
    )

    for _ in range(5):
        resolver.record_execution(
            scope_ref=scope_a, verb="act", op="deploy.restart", success=True
        )

    scope_a_ranked = resolver.rank(
        "act", "restart the deploy", _DESCRIPTORS, scope_ref=scope_a
    )
    scope_a_score = next(
        c.score for c in scope_a_ranked if c.descriptor.id == "deploy.restart"
    )
    scope_b_ranked = resolver.rank(
        "act", "restart the deploy", _DESCRIPTORS, scope_ref=scope_b
    )
    scope_b_score = next(
        c.score for c in scope_b_ranked if c.descriptor.id == "deploy.restart"
    )

    assert scope_a_score > baseline_score, (
        "repeated successful outcomes must raise the score for their own scope_ref"
    )
    assert scope_b_score < scope_a_score, (
        "a different tenant/policy scope_ref must not see scope_a's learned reward"
    )


@pytest.mark.spec("ARBORA-FLEET-R014")
def test_scope_ref_requires_verified_tenant_and_policy_revision() -> None:
    resolver = IntentResolver()
    with pytest.raises(ValueError):
        resolver.scope_ref("", "policy-1")
    with pytest.raises(ValueError):
        resolver.scope_ref("tenant-a", "")


@pytest.mark.spec("ARBORA-FLEET-R014")
def test_deterministic_uql_visibility_does_not_authorize_nl_fallback() -> None:
    resolution = IntentResolver().resolve(
        "ask",
        "why did the deploy fail",
        (Descriptor(id="query.uql", verb="ask", summary="Execute UQL"),),
        scope_ref=None,
    )
    assert resolution.op is None
    assert resolution.fallback is False


_UQL_DESCRIPTOR = Descriptor(id="query.uql", verb="ask", summary="Execute a UQL query")
_LOOKALIKE = Descriptor(
    id="deploy.owner_status",
    verb="ask",
    summary="who owns the deploy catalog status lineage",
    examples=("who owns these assets",),
)


@pytest.mark.spec("ARBORA-OPS-R855.1")
@pytest.mark.spec("ARBORA-OPS-R855.4")
@pytest.mark.spec("ARBORA-FLEET-R043.2")
@pytest.mark.parametrize(
    "question",
    [
        "which catalog assets mention customers",
        "who owns the orders table",
        "show the lineage of the revenue report",
        "what does the glossary say about churn",
        "which connector prompts are system-prompt candidates",
        "which data sources can I query",
    ],
)
def test_catalog_class_ask_routes_to_the_engine_planner_ahead_of_ranking(
    question: str,
) -> None:
    resolution = IntentResolver().resolve(
        "ask", question, (_LOOKALIKE, _UQL_DESCRIPTOR, _NL_DESCRIPTOR), scope_ref=None
    )

    assert resolution.op == "query.nl"
    assert resolution.params == {"text": question}
    assert resolution.preview is False
    assert resolution.alternatives == ("query.uql",)


@pytest.mark.spec("ARBORA-OPS-R855.5")
def test_find_for_catalog_assets_points_at_the_graph_query_ops_first() -> None:
    resolution = IntentResolver().resolve(
        "find",
        "find catalog assets about customers",
        (*_DESCRIPTORS, _NL_DESCRIPTOR, _UQL_DESCRIPTOR),
        scope_ref=None,
    )

    assert resolution.op is None
    assert resolution.alternatives[:2] == ("query.nl", "query.uql")


@pytest.mark.spec("ARBORA-OPS-R855.6")
def test_catalog_question_is_not_routed_without_a_visible_nl_planner() -> None:
    resolution = IntentResolver().resolve(
        "ask", "show the lineage of the revenue report", _DESCRIPTORS, scope_ref=None
    )

    assert resolution.op is None
    assert resolution.fallback is False


_ASK_DESCRIPTORS = (
    Descriptor(
        id="agents.tasks.get",
        verb="ask",
        summary="Show one agent task",
        examples=("show the task",),
        params_schema={"required": ["task_id"]},
    ),
    Descriptor(
        id="fleet.status",
        verb="ask",
        summary="Show fleet health",
        examples=("show fleet status",),
        params_schema={},
    ),
)


@pytest.mark.spec("ARBORA-FLEET-R014.1")
def test_ask_match_needing_a_required_param_yields_to_nl_planner() -> None:
    resolver = IntentResolver()
    question = "Which task tracks the open issues?"
    resolution = resolver.resolve(
        "ask", question, (_ASK_DESCRIPTORS[0], _NL_DESCRIPTOR), scope_ref=None
    )

    assert resolution.op == "query.nl"
    assert resolution.fallback is True
    assert resolution.params == {"text": question}
    assert "agents.tasks.get" in resolution.alternatives


@pytest.mark.spec("ARBORA-FLEET-R014.1")
def test_ask_prefers_the_next_runnable_match_over_one_missing_params() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "ask",
        "show the task fleet status",
        (*_ASK_DESCRIPTORS, _NL_DESCRIPTOR),
        scope_ref=None,
    )

    assert resolution.op == "fleet.status"
    assert resolution.missing_required == ()
    assert resolution.fallback is False


@pytest.mark.spec("ARBORA-FLEET-R014.1")
def test_ask_match_with_its_required_param_supplied_still_wins() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "ask",
        "show the task",
        (*_ASK_DESCRIPTORS, _NL_DESCRIPTOR),
        scope_ref=None,
        params={"task_id": "t1"},
    )

    assert resolution.op == "agents.tasks.get"
    assert resolution.missing_required == ()


@pytest.mark.spec("ARBORA-FLEET-R014.1")
def test_ask_without_nl_keeps_the_missing_params_preview() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "ask", "show the task", (_ASK_DESCRIPTORS[0],), scope_ref=None
    )

    assert resolution.op == "agents.tasks.get"
    assert resolution.missing_required == ("task_id",)


@pytest.mark.spec("ARBORA-FLEET-R014.2")
def test_a_request_verb_alone_is_not_evidence_for_an_ask_op() -> None:
    resolver = IntentResolver()
    question = "Show open issues across GitHub, GitLab and Plane"
    resolution = resolver.resolve(
        "ask", question, (_ASK_DESCRIPTORS[1], _NL_DESCRIPTOR), scope_ref=None
    )

    assert resolution.op == "query.nl"
    assert resolution.fallback is True


@pytest.mark.spec("ARBORA-FLEET-R014.2")
def test_a_named_op_still_matches_after_a_request_verb() -> None:
    resolver = IntentResolver()
    resolution = resolver.resolve(
        "ask",
        "show the fleet status",
        (_ASK_DESCRIPTORS[1], _NL_DESCRIPTOR),
        scope_ref=None,
    )

    assert resolution.op == "fleet.status"
