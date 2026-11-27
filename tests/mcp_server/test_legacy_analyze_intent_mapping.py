"""ARBORA-HOST-R005.2.1.1-.3: analyze actions resolve through the intent path.

Code navigation, impact and similarity are graph questions. They map onto
the registry's engine query operations instead of bespoke read operations.
"""

from __future__ import annotations

import pytest

from arbora.api.ops.query import operations as query_operations
from arbora.mcp_server.legacy_action_mapping import (
    APPROVED_ACTION_MAPPING,
    CODE_NAVIGATION_ACTIONS,
    IMPACT_ACTIONS,
    SIMILARITY_ACTIONS,
)


def _registry_ids() -> set[str]:
    return {op.id for op in query_operations()}


def _assert_resolves(actions: tuple[str, ...]) -> None:
    ids = _registry_ids()
    for action in actions:
        assert action in APPROVED_ACTION_MAPPING, action
        assert APPROVED_ACTION_MAPPING[action].operation in ids, action


@pytest.mark.spec("ARBORA-HOST-R005.2.1.1")
def test_code_navigation_actions_resolve_to_engine_query_ops() -> None:
    _assert_resolves(CODE_NAVIGATION_ACTIONS + ("explain",))


@pytest.mark.spec("ARBORA-HOST-R005.2.1.2")
def test_impact_actions_resolve_to_engine_query_ops() -> None:
    _assert_resolves(IMPACT_ACTIONS)


def test_similarity_action_resolves_to_engine_query_ops() -> None:
    _assert_resolves(SIMILARITY_ACTIONS)


@pytest.mark.spec("ARBORA-HOST-R005.2.1.3")
def test_remaining_analyze_actions_mapped() -> None:
    from arbora.api.ops.registry_factory import get_registry
    from arbora.mcp_server.legacy_action_mapping import (
        ANALYZE_QUERY_ACTIONS,
        resolve_legacy_action,
    )

    _assert_resolves(SIMILARITY_ACTIONS)
    for action in ANALYZE_QUERY_ACTIONS:
        assert resolve_legacy_action(action).operation == "query.uql", action
    registry = get_registry()
    for action, operation in (
        ("inspect", "ontology.inspect"),
        ("security_scan", "security.posture.read"),
        ("readiness", "ops.doctor"),
    ):
        assert resolve_legacy_action(action).operation == operation
        assert registry.get(operation) is not None
    retired = (
        "executable_rag",
        "recommend",
        "placement_plan",
        "infra_sweep",
        "distill_memory",
        "world_model_rollout",
        "research_ingest",
        "check_constraints",
    )
    for action in retired:
        resolved = resolve_legacy_action(action)
        assert resolved.operation is None and resolved.retired_by, action
