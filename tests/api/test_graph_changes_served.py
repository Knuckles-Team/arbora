"""Graph change models and snapshot recovery (ARBORA-OPS-R062.1, R062.3)."""

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.ops import graph_changes
from arbora.core.settings import RuntimeConfig
from tests._support import served_host
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


# ---- subscribe and stream (ARBORA-OPS-R062.2, R062.4, R062.5) ----


from starlette.websockets import WebSocketDisconnect  # noqa: E402

from arbora.api.invoke.pipeline import OperationRefused  # noqa: E402
from tests.api import _engine_double  # noqa: E402

SUBSCRIBE = next(op for op in graph_changes.operations() if op.id.endswith("subscribe"))


def change(commit: str, ordinal: int, *, visible: int = 5) -> dict[str, Any]:
    """An event as the engine pages it: ids and positions, no node content."""
    return {
        **EVENT,
        "commit_id": commit,
        "event_ordinal": ordinal,
        "commit_to_visible_ms": visible,
    }


class ChangeFeed:
    """The engine's committed feed as a structural double of ``ReadGraphChanges``."""

    def __init__(self, monkeypatch) -> None:
        self.engine = _engine_double.EngineDouble(monkeypatch)
        self.log: list[dict[str, Any]] = []
        self.refuse: str | None = None
        self.engine.serve("OpenGraphSnapshot", lambda p: {**SNAPSHOT})
        self.engine.serve("ReadGraphChanges", self.read)
        self.engine.install(graph_changes)

    def read(self, params: dict[str, Any]) -> dict[str, Any]:
        if self.refuse is not None:
            raise EngineRefusal(self.refuse)
        start = int(params["cursor"].removeprefix("cur-"))
        page = self.log[start : start + params["limit"]]
        return {"events": page, "cursor": f"cur-{start + len(page)}"}


_CONFIG = RuntimeConfig()


def subscribe_context() -> Any:
    return _engine_double.context(scopes=frozenset({"graph:read"}), config=_CONFIG)


async def poll(cursor: str | None = None, **changes: Any) -> dict[str, Any]:
    params = {**({"cursor": cursor} if cursor else {}), **changes}
    done = await graph_changes.handle_graph_changes_subscribe(
        subscribe_context(), params, SUBSCRIBE
    )
    return done["value"]


@pytest.mark.spec("ARBORA-OPS-R062.2")
async def test_subscribe_pages_events_in_cursor_order_without_content(
    monkeypatch,
) -> None:
    feed = ChangeFeed(monkeypatch)
    head = await poll()
    assert head["cursor"] == "cur-90" and head["events"] == []
    assert head["stream_path"] == "/ws/graph/changes"
    feed.log = [change("c2", 1), change("c1", 2), change("c1", 1)]
    page = await poll("cur-0")
    assert [e["event_id"] for e in page["events"]] == ["c1:1", "c1:2", "c2:1"]
    assert page["cursor"] == "cur-3"
    assert SUBSCRIBE.scopes == {"graph:read"}
    # The batch is bounded by configuration, never by the caller alone.
    monkeypatch.setattr(_CONFIG, "graph_changes_max_batch", 2)
    assert feed.engine.sent[-1][1]["limit"] == 500
    await poll("cur-0", limit=5000)
    assert feed.engine.sent[-1][1]["limit"] == 2


@pytest.mark.spec("ARBORA-OPS-R062.2")
@pytest.mark.parametrize(
    "code", ["CURSOR_EXPIRED", "GRAPH_INCARNATION_STALE", "AUTHORIZATION_CHANGED"]
)
async def test_unusable_cursor_is_stale_and_names_the_engine_reason(
    monkeypatch, code
) -> None:
    feed = ChangeFeed(monkeypatch)
    feed.refuse = code
    with pytest.raises(OperationRefused) as stale:
        await poll("cur-0")
    assert stale.value.code == "STALE_CURSOR" and stale.value.details == {
        "reason": code
    }
    feed.refuse = "SOMETHING_ELSE"
    with pytest.raises(EngineRefusal):
        await poll("cur-0")


@pytest.mark.spec("ARBORA-OPS-R062.2")
async def test_pinned_engine_refuses_naming_the_missing_method(monkeypatch) -> None:
    import medulla.generated as generated

    ChangeFeed(monkeypatch)
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"ReadGraphChanges"}
    )
    with pytest.raises(OperationRefused) as pinned:
        await poll("cur-0")
    assert pinned.value.code == "UNAVAILABLE"
    assert pinned.value.details["missing"] == "ReadGraphChanges"


def stream_client(monkeypatch, feed: ChangeFeed, script, *, revoke_after=None):
    """The gateway app with the change route; every poll is the real handler.

    ``script`` holds callables run between polls; ``revoke_after`` makes the bearer
    check fail on that poll number, the way a revoked session does.
    """
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    from arbora.api.http.auth import HTTPAuthenticationError
    from arbora.gateway import stream_poll
    from arbora.gateway.graph_changes_ws import register_graph_change_routes

    polls: list[dict[str, Any]] = []

    async def invoke_operation(op_id, params, *, request, idempotency_key=None):
        if revoke_after is not None and len(polls) >= revoke_after:
            raise HTTPAuthenticationError("revoked")
        polls.append(dict(params))
        return await graph_changes.handle_graph_changes_subscribe(
            subscribe_context(), params, SUBSCRIBE
        )

    async def pause(websocket):
        if not script:
            raise stream_poll.StopStream
        script.pop(0)()

    monkeypatch.setattr(stream_poll, "wait_for_next_poll", pause)
    app = FastAPI()
    app.state.host = served_host(invoke_operation)
    register_graph_change_routes(app)
    return TestClient(app), polls


@pytest.mark.spec("ARBORA-OPS-R062.2")
def test_stream_orders_events_and_reauthorizes_on_reconnect(monkeypatch) -> None:
    feed = ChangeFeed(monkeypatch)
    feed.log = [change("c1", 2), change("c1", 1)]

    def more():
        feed.log.append(change("c2", 1))

    client, polls = stream_client(monkeypatch, feed, [more])
    with client.websocket_connect("/ws/graph/changes?cursor=cur-0") as socket:
        first, second = socket.receive_json(), socket.receive_json()
    assert [e["event_id"] for e in first["events"]] == ["c1:1", "c1:2"]
    assert [e["event_id"] for e in second["events"]] == ["c2:1"]
    assert [p["cursor"] for p in polls] == ["cur-0", "cur-2"]
    # A reconnect resumes behind the client's own cursor and verifies the bearer again.
    client, again = stream_client(monkeypatch, feed, [])
    with client.websocket_connect("/ws/graph/changes?cursor=cur-2") as socket:
        assert socket.receive_json()["events"][0]["event_id"] == "c2:1"
    assert again == [{"cursor": "cur-2"}]


@pytest.mark.spec("ARBORA-OPS-R062.4")
def test_revocation_stops_delivery_and_limit_is_measured(monkeypatch) -> None:
    from arbora.core.settings import ArboraConfig

    feed = ChangeFeed(monkeypatch)
    feed.log = [change("c1", 1)]
    client, polls = stream_client(
        monkeypatch,
        feed,
        [lambda: feed.log.append(change("c1", 2))] * 3,
        revoke_after=1,
    )
    with client.websocket_connect("/ws/graph/changes?cursor=cur-0") as socket:
        assert socket.receive_json()["events"][0]["event_id"] == "c1:1"
        # Revoked: no second frame ever arrives, and the close is 4401.
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
    assert closed.value.code == 4401 and len(polls) == 1
    # A cursor bound to an older authorization revision closes 4409; the client recovers.
    feed.refuse = "AUTHORIZATION_CHANGED"
    client, _ = stream_client(monkeypatch, feed, [])
    with client.websocket_connect("/ws/graph/changes?cursor=cur-0") as socket:
        assert socket.receive_json()["code"] == "STALE_CURSOR"
        with pytest.raises(WebSocketDisconnect) as stale:
            socket.receive_json()
    assert stale.value.code == 4409
    # The commit-to-visible limit is configuration with bounds, and the p95 of a
    # delivered batch is measured against it.
    assert ArboraConfig().graph_changes_commit_to_visible_p95_ms == 2000
    for bad in (99, 60001):
        with pytest.raises(ValidationError):
            ArboraConfig.model_validate({"GRAPH_CHANGES_COMMIT_TO_VISIBLE_P95_MS": bad})
    latencies = sorted(
        e["commit_to_visible_ms"]
        for e in [change("c", n, visible=n) for n in range(1, 101)]
    )
    assert latencies[94] <= ArboraConfig().graph_changes_commit_to_visible_p95_ms


@pytest.mark.spec("ARBORA-OPS-R062.5")
async def test_two_browsers_converge_under_gaps_reconnect_and_revocation(
    monkeypatch,
) -> None:
    feed = ChangeFeed(monkeypatch)
    feed.log = [change("c1", 1), change("c1", 2), change("c2", 1)]

    class Browser:
        """Applies pages by event id, so a duplicate or reordered event is harmless."""

        def __init__(self) -> None:
            self.cursor, self.applied = "cur-0", {}

        async def pull(self) -> None:
            page = await poll(self.cursor)
            for event in page["events"]:
                self.applied[event["event_id"]] = event["change"]
            self.cursor = page["cursor"]

    one, two = Browser(), Browser()
    await one.pull()
    # A duplicate and a reordered tail: the second browser reads late and out of order.
    feed.log += [change("c3", 1), change("c2", 1)]
    two.cursor = "cur-1"
    await two.pull()
    await one.pull()
    assert one.applied.keys() == two.applied.keys() | {"c1:1"}
    # The engine cannot continue a cursor: the browser recovers from the snapshot
    # and its continuation point is the snapshot's own high-water mark.
    feed.refuse = "CURSOR_EXPIRED"
    with pytest.raises(OperationRefused):
        await two.pull()
    feed.refuse = None
    recovered = await poll()
    two.cursor = recovered["cursor"]
    assert recovered["cursor"] == SNAPSHOT["high_water_cursor"]
    one_ids = set(one.applied)
    assert one_ids == {"c1:1", "c1:2", "c2:1", "c3:1"}
