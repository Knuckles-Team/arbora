"""ARBORA-HOST-R005.2.x: every frozen legacy MCP action resolves to one operation or one owner row."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from arbora.api.ops.registry_factory import get_registry
from arbora.mcp_server.legacy_action_mapping import (
    APPROVED_ACTION_MAPPING,
    UnmappedActionError,
    resolve_legacy_action,
    validate_served_actions,
)

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_mcp_actions.json"
_ROW = re.compile(r"^[A-Z]+(?:-[A-Z0-9]+)*(?:-R\d+(?:\.\d+)*)?$")


def _family(name: str) -> dict[str, list[str]]:
    return json.loads(FIXTURE.read_text())[name]


def _assert_family_resolves(name: str) -> None:
    registry = get_registry()
    family = _family(name)
    assert family
    for tool, actions in family.items():
        assert actions, tool
        for action in actions:
            resolved = resolve_legacy_action(action, tool)
            assert (resolved.operation is None) != (resolved.retired_by is None)
            if resolved.operation is not None:
                assert registry.get(resolved.operation) is not None, (
                    tool,
                    action,
                    resolved.operation,
                )
            else:
                assert _ROW.match(resolved.retired_by or ""), (tool, action)


def _operation(action: str, tool: str | None = None) -> str | None:
    return resolve_legacy_action(action, tool).operation


def _unmapped(families: dict[str, dict[str, list[str]]]) -> list[str]:
    """Fixture actions the table lacks, by qualified or bare key."""
    return [
        f"{tool}.{action}"
        for family in families.values()
        for tool, actions in family.items()
        for action in actions
        if action not in APPROVED_ACTION_MAPPING
        and f"{tool}.{action}" not in APPROVED_ACTION_MAPPING
    ]


@pytest.mark.spec("ARBORA-HOST-R005.2.8")
def test_every_legacy_action_has_resolvable_mapping() -> None:
    families = json.loads(FIXTURE.read_text())
    assert set(families) == {
        "analyze",
        "configure",
        "ingest",
        "query-memory",
        "orchestrate",
        "mine-learn",
        "governance-ops",
    }
    for name in families:
        _assert_family_resolves(name)
    assert not _unmapped(families)
    validate_served_actions(("explain",), APPROVED_ACTION_MAPPING)


@pytest.mark.spec("ARBORA-HOST-R005.2.8")
def test_an_action_missing_from_the_table_fails_the_parity_check() -> None:
    with pytest.raises(UnmappedActionError):
        resolve_legacy_action("an_action_the_retired_host_never_had", "graph_analyze")


@pytest.mark.spec("ARBORA-HOST-R005.2.2")
def test_family_configure_mapped_and_resolvable() -> None:
    _assert_family_resolves("configure")
    for action in ("describe", "get", "get_config", "list_config", "generate_config"):
        assert _operation(action, "graph_configure") == "ops.config"
    for action in ("config_doctor", "health", "system_doctor", "preflight", "doctor"):
        assert _operation(action, "graph_configure") == "ops.doctor"
    for action in ("set_secret", "vault_sync"):
        resolved = resolve_legacy_action(action, "graph_configure")
        assert resolved.operation == "fleet.call"
        assert dict(resolved.params)["server"] == "openbao-mcp"
    assert _operation("install_hooks") == "ops.hooks.install"
    assert _operation("uninstall_hooks") == "ops.hooks.uninstall"


@pytest.mark.spec("ARBORA-HOST-R005.2.3.3")
def test_corpus_mapped() -> None:
    assert _operation("corpus") == "ingest.sources.list"
    assert resolve_legacy_action("corpus_build").retired_by == "MEDULLA-INGEST-WORKER"


@pytest.mark.spec("ARBORA-HOST-R005.2.3.5")
def test_rebuild_and_observe_mapped() -> None:
    assert _operation("rebuild_indexes") == "ingest.repositories.index"
    assert _operation("observe") == "telemetry.read"
    registry = get_registry()
    assert registry.get("ingest.repositories.index") is not None
    assert registry.get("telemetry.read") is not None


@pytest.mark.spec("ARBORA-HOST-R005.2.3.6")
def test_materialize_actions_mapped() -> None:
    for action in ("materialize", "materialize_source"):
        resolved = resolve_legacy_action(action)
        assert resolved.operation == "ingest.sources.sync"
        assert dict(resolved.params) == {"mode": "full"}


@pytest.mark.spec("ARBORA-HOST-R005.2.3.7")
def test_reflect_and_toolkit_mapped() -> None:
    assert resolve_legacy_action("reflect").retired_by == "MEDULLA-DECISION-ENGINE"
    assert _operation("agent_toolkit") == "fleet.catalog.list"
    assert get_registry().get("fleet.catalog.list") is not None


@pytest.mark.spec("ARBORA-HOST-R005.2.3.8")
def test_family_ingest_mapped_and_resolvable() -> None:
    _assert_family_resolves("ingest")
    listed = set(_family("ingest")["graph_ingest"])
    for action in (
        "sync",
        "profile",
        "import_pack",
        "curate_wiki",
        "sync_second_brain",
    ):
        assert action in listed
    assert any(a.startswith("cohort_") for a in listed)
    assert any(a.startswith("extract_") for a in listed)
    assert "skill_workflows" in listed
    assert "build_skill_graph" in listed


@pytest.mark.spec("ARBORA-HOST-R005.2.4")
def test_family_query_memory_mapped_and_resolvable() -> None:
    _assert_family_resolves("query-memory")
    assert _operation("recall_memory") == "memory.read"
    assert _operation("submit_sdd") == "agents.plan_run"
    assert _operation("recall", "graph_memory") == "memory.read"
    assert _operation("store", "graph_memory") == "memory.write"
    assert _operation("link", "graph_memory") == "memory.write"
    assert _operation("sql", "graph_query") == "query.sql"
    assert _operation("sparql", "graph_query") == "query.sparql"
    assert _operation("add_node", "graph_write") == "eg.graph.AddNode"


@pytest.mark.spec("ARBORA-HOST-R005.2.5")
def test_family_orchestrate_mapped_and_resolvable() -> None:
    _assert_family_resolves("orchestrate")
    assert _operation("execute_agent", "graph_orchestrate") == "agents.run"
    assert _operation("list", "graph_goals") == "agents.goals.list"
    # Scheduling and goal creation are choices: retired to the decision engine.
    assert resolve_legacy_action("submit", "graph_loops").retired_by == (
        "MEDULLA-DECISION-ENGINE"
    )
    assert resolve_legacy_action("create", "graph_goals").retired_by == (
        "MEDULLA-DECISION-ENGINE"
    )


@pytest.mark.spec("ARBORA-HOST-R005.2.6")
def test_family_mine_learn_mapped_and_resolvable() -> None:
    _assert_family_resolves("mine-learn")
    assert _operation("cluster", "graph_mine") == "eg.compute.MineCluster"
    assert _operation("classify_fit", "graph_mine") == "eg.compute.MineClassifyFit"
    assert _operation("fit", "graph_learn") == "eg.compute.GraphLearnFit"
    deep = resolve_legacy_action("deep_forecast", "graph_mine_deep")
    assert dict(deep.params)["server"] == "data-science-mcp"
    # Evaluation verdicts are medulla choices.
    assert resolve_legacy_action("evaluate", "graph_evaluate").retired_by is not None


@pytest.mark.spec("ARBORA-HOST-R005.2.7")
def test_family_governance_ops_mapped_and_resolvable() -> None:
    _assert_family_resolves("governance-ops")
    assert _operation("verify", "graph_audit") == "eg.security.AuditVerify"
    assert _operation("verify_action", "graph_governance") == "access.check"
    assert _operation("policy_status", "graph_governance") == "policy.read"
    assert _operation("posture", "graph_compliance") == "security.posture.read"
    assert _operation("send", "graph_bus") == "bus.send"
