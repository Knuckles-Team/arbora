"""Graph change models and snapshot recovery (ARBORA-OPS-R062.1, R062.3)."""

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.ops import graph_changes
from tests.api._lane_double import EngineDouble, context

OP = graph_changes.operations()[0]
SNAPSHOT = {
    "schema_version": 1,
    "incarnation": 4,
    "committed_revision": 90,
    "snapshot_digest": "d" * 64,
    "visibility_revision": 7,
    "high_water_cursor": "cur-90",
}
EVENT = {
    "schema_version": 1,
    "commit_id": "c1",
    "event_ordinal": 3,
    "committed_at_ms": 10,
    "visible_at_ms": 12,
    "commit_to_visible_ms": 2,
    "change": {"kind": "node_upserted"},
}


def _serve(monkeypatch: pytest.MonkeyPatch) -> None:
    import medulla.generated as generated

    from arbora.pin_lag import medulla as pin_lag

    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"OpenGraphSnapshot", "ReadGraphChanges"},
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


@pytest.mark.spec("ARBORA-OPS-R062.1")
def test_models_round_trip_rust_vectors() -> None:
    event = graph_changes.GraphChangeEvent(**EVENT)
    assert event.model_dump() == EVENT
    snapshot = graph_changes.GraphSnapshotRecovery(**SNAPSHOT)
    assert snapshot.model_dump() == SNAPSHOT
    for bad in (
        {**EVENT, "event_ordinal": -1},
        {**EVENT, "extra": 1},
        {**EVENT, "commit_id": ""},
    ):
        with pytest.raises(ValidationError):
            graph_changes.GraphChangeEvent(**bad)
    with pytest.raises(ValidationError):
        graph_changes.GraphSnapshotRecovery(**{**SNAPSHOT, "high_water_cursor": ""})


@pytest.mark.spec("ARBORA-OPS-R062.3")
@pytest.mark.asyncio
async def test_expired_cursor_returns_typed_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    for code in ("CURSOR_EXPIRED", "GRAPH_INCARNATION_STALE", "AUTHORIZATION_CHANGED"):
        client = EngineDouble(
            ReadGraphChanges=EngineResponseError(code, "no"), OpenGraphSnapshot=SNAPSHOT
        )
        answer = await graph_changes.handle_graph_snapshot_recover(
            context(client, monkeypatch), {"cursor": "old"}, OP
        )
        assert answer["state"] == "recover" and answer["cursor"] == "cur-90"
        assert answer["snapshot"] == SNAPSHOT
        assert [sent[0] for sent in client.sent] == [
            "ReadGraphChanges",
            "OpenGraphSnapshot",
        ]
    # A cursor the engine still accepts needs no recovery and opens no snapshot.
    live = EngineDouble(ReadGraphChanges={"events": [], "next_cursor": "n"})
    kept = await graph_changes.handle_graph_snapshot_recover(
        context(live, monkeypatch), {"cursor": "ok"}, OP
    )
    assert kept == {"state": "continue", "cursor": "ok", "snapshot": None}
    assert [sent[0] for sent in live.sent] == ["ReadGraphChanges"]
    # No cursor asks for a snapshot outright; an unrelated refusal is not a recovery.
    fresh = EngineDouble(OpenGraphSnapshot=SNAPSHOT)
    first = await graph_changes.handle_graph_snapshot_recover(
        context(fresh, monkeypatch), {}, OP
    )
    assert first["state"] == "recover"
    broken = EngineDouble(ReadGraphChanges=EngineResponseError("ACCESS_DENIED", "x"))
    with pytest.raises(EngineRefusal) as refused:
        await graph_changes.handle_graph_snapshot_recover(
            context(broken, monkeypatch), {"cursor": "c"}, OP
        )
    assert refused.value.code == "ACCESS_DENIED"
