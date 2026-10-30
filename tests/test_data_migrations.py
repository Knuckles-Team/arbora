"""Versioned data migrations: type-only ActionDecision rows are labelled in place.

The native tests run the released engine selected by
ARBORA_NOAUTH_TEST_ENGINE_BINARY (skipped without it); the rest use doubles.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.access.action_policy import (
    DENIAL_LEDGER_UNREADABLE,
    DENIAL_RATE_LIMITED,
    ActionPolicy,
    ActionRequest,
    LedgerUnreadable,
)
from arbora.data_migrations import (
    DATA_MIGRATIONS,
    DataMigration,
    MigrationResult,
    run_data_migrations,
)
from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_served_engine_native_labels import native_engine as native_engine

pytestmark = pytest.mark.spec("ARBORA-HOST-R033.2")

_MIGRATION = "0001-action-decision-node-type-label"


def _legacy_row(node_id: str, kind: str, target: str) -> dict[str, Any]:
    """An ActionDecision as arbora <= 0.1.8.dev4 stored it: ``type`` only."""
    return {
        "id": node_id,
        "type": "ActionDecision",
        "kind": kind,
        "target": target,
        "params_json": "{}",
        "source": "fleet",
        "tier": "auto_notify",
        "decision": "allow_notify",
        "reason": "tier auto_notify",
        "rule_origin": "file",
        "approval_id": "",
        "request_digest": "d" * 64,
        "receipt_schema": "arbora.policy-receipt.v1",
        "decided_at": "2026-10-20T01:02:03+00:00",
        "decided_unix": 1.0,
    }


def _props(engine: Any, node_id: str) -> dict[str, Any]:
    rows = engine.query_cypher("MATCH (n {id: $id}) RETURN n", {"id": node_id})
    return dict(rows[0]["n"])


def test_native_migration_labels_rows_in_place_and_is_recorded(native_engine):
    engine, session = native_engine
    client = engine.graph_compute.for_graph(session.graph).async_client
    legacy = {
        f"legacy-{i}": _legacy_row(f"legacy-{i}", "connector_pack_import", f"c{i}")
        for i in range(3)
    }
    for node_id, row in legacy.items():
        assert asyncio.run(client.nodes.create_if_absent(node_id, row))
    policy = ActionPolicy(engine)
    request = ActionRequest(kind="connector_pack_import", target="c9", source="fleet")

    unreadable = policy._recent_decisions("connector_pack_import")
    assert unreadable == LedgerUnreadable(
        reason=(
            "ledger unreadable: 3 rows lack label ActionDecision "
            "(kind connector_pack_import)"
        ),
        unlabeled=3,
    )

    reports = run_data_migrations(engine)
    assert reports == [
        {
            "migration_id": _MIGRATION,
            "status": "applied",
            "found": 3,
            "changed": 3,
            "remaining": 0,
        }
    ]
    for node_id, row in legacy.items():
        # Exactly one field added; every original field and timestamp kept.
        assert _props(engine, node_id) == {**row, "node_type": "ActionDecision"}
    rows = policy._recent_decisions("connector_pack_import")
    assert isinstance(rows, list)
    assert {r["id"] for r in rows} == set(legacy)
    assert not policy._rate_exceeded(request, {"max": 6, "window_s": 3600})

    record = engine.query_cypher(
        "MATCH (m:DataMigration {migration_id: $id}) "
        "RETURN m.found AS found, m.changed AS changed, m.remaining AS remaining",
        {"id": _MIGRATION},
    )
    assert record == [{"found": 3, "changed": 3, "remaining": 0}]
    # Recorded: the next start skips it and changes nothing.
    assert run_data_migrations(engine) == [
        {"migration_id": _MIGRATION, "status": "recorded"}
    ]
    # The step itself is idempotent even when run again unrecorded.
    assert DATA_MIGRATIONS[0].apply(engine, _MIGRATION) == MigrationResult(0, 0, 0)


def test_native_unreadable_ledger_denial_names_the_cause(native_engine):
    engine, session = native_engine
    client = engine.graph_compute.for_graph(session.graph).async_client
    assert asyncio.run(
        client.nodes.create_if_absent(
            "legacy-observe", _legacy_row("legacy-observe", "observe", "t")
        )
    )
    decision = ActionPolicy(engine).decide(
        ActionRequest(kind="observe", target="t", source="qualification")
    )
    assert not decision.allowed
    assert decision.denial == DENIAL_LEDGER_UNREADABLE
    assert decision.reason == (
        "ledger unreadable: 1 rows lack label ActionDecision (kind observe)"
    )
    assert "rate limit" not in decision.reason


class _Engine:
    """Records reads and CAS calls; answers from scripted rows."""

    def __init__(self, unlabeled: list[str], cas_applies: bool = True) -> None:
        self.unlabeled = list(unlabeled)
        self.cas_applies = cas_applies
        self.created: list[tuple[str, str, dict[str, Any]]] = []

    def query_cypher(self, query: str, params: Any = None) -> list[Any]:
        if "DataMigration" in query:
            return [{"id": c[0]} for c in self.created if c[1] == "DataMigration"]
        if "count(n)" in query:
            return [{"n": len(self.unlabeled)}]
        return [{"id": node_id} for node_id in self.unlabeled]

    def compare_and_set_node(self, node_id, conditions, updates, *, idempotency_key):
        assert conditions == {"type": "ActionDecision", "node_type": None}
        assert updates == {"node_type": "ActionDecision"}
        assert idempotency_key == f"arbora:data-migration:{_MIGRATION}:{node_id}"
        if self.cas_applies:
            self.unlabeled.remove(node_id)
        return self.cas_applies

    def create_node(self, node_id: str, label: str, properties: Any) -> bool:
        self.created.append((node_id, label, dict(properties)))
        return True


def test_rows_left_behind_are_reported_and_not_recorded():
    engine = _Engine(["a", "b"], cas_applies=False)
    reports = run_data_migrations(engine)
    assert reports == [
        {
            "migration_id": _MIGRATION,
            "status": "incomplete",
            "found": 2,
            "changed": 0,
            "remaining": 2,
        }
    ]
    assert engine.created == []


def test_a_failing_migration_is_reported_and_never_raises():
    def boom(engine: Any, migration_id: str) -> MigrationResult:
        raise RuntimeError("CONFLICT: refused")

    failing = DataMigration("9999-x", "X", "0" * 64, "boom", boom)
    assert run_data_migrations(_Engine([]), [failing]) == [
        {
            "migration_id": "9999-x",
            "status": "failed",
            "error": "RuntimeError: CONFLICT: refused",
        }
    ]


def test_an_engine_without_compare_and_set_runs_nothing():
    assert run_data_migrations(object()) == []


@pytest.mark.spec("ARBORA-FLEET-R015.3")
def test_spent_budget_and_unreadable_ledger_are_distinct_denials(tmp_path):
    policy_file = tmp_path / "policy.yml"
    policy_file.write_text(
        "version: 1\n"
        "defaults: {tier: approval_required}\n"
        "rules:\n"
        "  - {kind: connector_pack_import, target: '*', tier: auto_notify,"
        " rate_limit: {max: 6, window_s: 3600}}\n"
    )

    class _Ledger:
        def __init__(self, unlabeled: int, rows: list[dict[str, Any]]) -> None:
            self.unlabeled, self.rows = unlabeled, rows

        def query_cypher(self, query: str, params: Any = None) -> list[Any]:
            if "count(d)" in query:
                return [{"n": self.unlabeled}]
            if "governance_rule" in query:
                return []
            return self.rows

        def create_node(self, *args: Any) -> bool:
            return True

    import time

    request = ActionRequest(kind="connector_pack_import", target="tm", source="fleet")
    spent = [
        {"id": str(i), "target": "tm", "decision": "allow_notify", "ts": time.time()}
        for i in range(6)
    ]
    policy = ActionPolicy(_Ledger(0, spent), policy_path=policy_file)
    limited = policy.decide(request)
    assert limited.denial == DENIAL_RATE_LIMITED
    assert limited.reason.startswith("rate limit exceeded")

    unreadable = ActionPolicy(_Ledger(273, []), policy_path=policy_file).decide(request)
    assert unreadable.denial == DENIAL_LEDGER_UNREADABLE
    assert unreadable.reason == (
        "ledger unreadable: 273 rows lack label ActionDecision "
        "(kind connector_pack_import)"
    )
