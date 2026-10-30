"""Gate: a persisted record's label or shape changes only with a data migration.

Each covered type has one writer. The writer's current shape (label plus
field names, :func:`arbora.data_migrations.shape_digest`) must equal the
``shape_digest`` the label's newest migration declares. Changing the writer,
or the one label model in :func:`arbora.unique_records.record_body`, fails
here until a new migration for that label lands in the same change.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.access.action_policy import (
    ACTION_DECISION_LABEL,
    ActionDecision,
    ActionPolicy,
    ActionRequest,
    action_decision_record,
)
from arbora.data_migrations import DATA_MIGRATIONS, current_shapes, shape_digest
from arbora.mcp_server.served_engine import ServedEngine
from arbora.unique_records import record_body

pytestmark = pytest.mark.spec("ARBORA-HOST-R033.3")


def test_every_covered_writer_shape_has_its_migration():
    newest = {m.label: m for m in DATA_MIGRATIONS}
    for label, digest in current_shapes().items():
        migration = newest.get(label)
        assert migration is not None, f"{label} has no data migration"
        assert migration.shape_digest == digest, (
            f"the persisted shape of {label} changed ({digest}) but its newest "
            f"migration {migration.migration_id} declares {migration.shape_digest}: "
            "add a data migration for the new shape in this change"
        )


def test_migration_ids_are_unique_and_ordered():
    ids = [m.migration_id for m in DATA_MIGRATIONS]
    assert len(ids) == len(set(ids))
    assert ids == sorted(ids)


def test_the_label_model_is_part_of_the_shape():
    body = record_body("X", {"a": 1})
    assert body == {"a": 1, "type": "X", "node_type": "X"}
    assert shape_digest("X", body) != shape_digest("X", {"a": 1, "type": "X"})


@pytest.mark.spec("ARBORA-HOST-R033.1")
def test_the_one_action_decision_writer_stores_the_label():
    written: list[tuple[str, str, dict[str, Any]]] = []

    class _Engine:
        def query_cypher(self, query: str, params: Any = None) -> list[Any]:
            return [{"n": 0}] if "count(d)" in query else []

        def create_node(self, node_id: str, label: str, properties: Any) -> bool:
            written.append((node_id, label, dict(properties)))
            return True

    decision = ActionPolicy(_Engine()).decide(
        ActionRequest(kind="observe", target="t", source="test")
    )
    [(node_id, label, props)] = written
    assert node_id == decision.audit_id and label == ACTION_DECISION_LABEL
    sample = ActionDecision(decision="x", tier="y", request=ActionRequest("k", "t"))
    assert set(props) == set(action_decision_record(sample))


@pytest.mark.spec("ARBORA-HOST-R033.1")
def test_served_engine_writes_build_their_body_from_the_label_model(monkeypatch):
    import msgpack

    sent: list[dict[str, Any]] = []

    async def capture(client, params, graph, *, idempotency_key=None):
        sent.append(msgpack.unpackb(params["properties_msgpack"]))
        return True

    import epistemic_graph.generated.graph as generated

    monkeypatch.setattr(generated, "send_create_node_if_absent", capture)
    monkeypatch.setattr(generated, "send_add_node", capture)

    class _Client:
        graph_name = "g"

    engine = ServedEngine.__new__(ServedEngine)
    monkeypatch.setattr(engine, "_session_client", lambda: _Client(), raising=False)
    engine.create_node("a", "ActionDecision", {"kind": "k"})
    engine.add_node("b", "ActionDecision", {"kind": "k"})
    assert sent == [record_body("ActionDecision", {"kind": "k"})] * 2
