"""Agent bus operations over the engine roster, topic and work-item methods (ARBORA-OPS-R882)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import bus
from arbora.api.registry import Effect
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble, context

_OPS = {op.id: op for op in bus.operations()}


class _Submitter:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    async def submit(self, **item: Any) -> dict[str, Any]:
        self.items.append(item)
        return {"work_item_id": f"wi-{len(self.items)}"}


def _ctx(tenant: str = "tenant:one", **services: Any) -> Any:
    ctx = context(tenant=tenant)
    ctx.services = {"clock": lambda: 1_000, **services}
    return ctx


class _Bus:
    """The engine roster and topics, as one structural double."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.roster: list[dict[str, Any]] = []
        self.topics: dict[str, list[dict[str, Any]]] = {}
        self.engine = EngineDouble(monkeypatch)
        self.engine.serve("BusRegister", self._register)
        self.engine.serve("BusHeartbeat", lambda p: {"seen": p["now_ms"]})
        self.engine.serve("BusRoster", lambda p: {"participants": self.roster})
        self.engine.serve("TopicAppend", self._append)
        self.engine.serve(
            "TopicRead", lambda p: {"messages": self.topics.get(p["topic"], [])}
        )
        self.engine.install(bus)
        self.engine.install(pin_lag)

    def _register(self, p: dict[str, Any]) -> Any:
        self.roster.append(dict(p["participant"]))
        return {"registered": p["participant"]["session_ref"]}

    def _append(self, p: dict[str, Any]) -> Any:
        self.topics.setdefault(p["topic"], []).append(
            {"sender": p["sender"], "payload": p["payload"]}
        )
        return {"offset": len(self.topics[p["topic"]])}


@pytest.mark.spec("ARBORA-OPS-R882")
def test_the_six_operations_are_registered_with_bus_scopes() -> None:
    assert sorted(_OPS) == [
        "bus.dispatch",
        "bus.heartbeat",
        "bus.receive",
        "bus.register",
        "bus.roster",
        "bus.send",
    ]
    for op_id in ("bus.roster", "bus.receive"):
        assert _OPS[op_id].effect is Effect.READ
        assert _OPS[op_id].scopes == frozenset({"bus:read"})
    for op_id in ("bus.register", "bus.heartbeat", "bus.send", "bus.dispatch"):
        assert _OPS[op_id].effect is Effect.WRITE
        assert _OPS[op_id].scopes == frozenset({"bus:write"})
        assert op_id in bus.NATIVE_IDEMPOTENCY


@pytest.mark.spec("ARBORA-OPS-R882")
async def test_a_pinned_engine_without_the_topic_methods_refuses_naming_them() -> None:
    from medulla.generated import METHOD_IDS

    if "TopicAppend" in METHOD_IDS:
        pytest.skip("the pin carries the topic methods")
    with pytest.raises(OperationRefused) as refused:
        await bus.handle_bus_receive(
            _ctx(), {"session_ref": "s1", "limit": 5}, _OPS["bus.receive"]
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "TopicRead"


@pytest.mark.spec("ARBORA-OPS-R882")
async def test_send_receive_dispatch_over_medulla(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _Bus(monkeypatch)
    submitter = _Submitter()
    ctx = _ctx(work_submitter=submitter)
    for ref in ("sender", "worker"):
        await bus.handle_bus_register(
            ctx,
            {"session_ref": ref, "host_ref": "host-1", "capabilities": ["search"]},
            _OPS["bus.register"],
        )
    # A participant of another tenant is on the roster the engine returns but is never reachable.
    engine.roster.append(
        {
            "session_ref": "foreign",
            "tenant": "tenant:two",
            "host_ref": "h",
            "capabilities": [],
        }
    )
    beat = await bus.handle_bus_heartbeat(
        ctx, {"session_ref": "worker"}, _OPS["bus.heartbeat"]
    )
    roster = await bus.handle_bus_roster(ctx, {}, _OPS["bus.roster"])
    assert beat["value"]["seen"] == 1_000
    assert [p["session_ref"] for p in roster["value"]["participants"]] == [
        "sender",
        "worker",
    ]

    await bus.handle_bus_send(
        ctx, {"recipient": "worker", "body": "hello"}, _OPS["bus.send"]
    )
    received = await bus.handle_bus_receive(
        ctx, {"session_ref": "worker", "limit": 10}, _OPS["bus.receive"]
    )
    assert received["value"]["messages"] == [
        {"sender": "principal:one", "payload": "hello"}
    ]
    dispatched = await bus.handle_bus_dispatch(
        ctx,
        {"recipient": "worker", "objective": "index the docs"},
        _OPS["bus.dispatch"],
    )
    assert dispatched["value"]["work_item_id"] == "wi-1"
    assert submitter.items[0]["tenant"] == "tenant:one"

    # A foreign or unknown recipient answers identically, before any effect.
    sent = len(engine.engine.sent)
    for recipient in ("foreign", "nobody"):
        with pytest.raises(OperationRefused) as refused:
            await bus.handle_bus_send(
                ctx, {"recipient": recipient, "body": "x"}, _OPS["bus.send"]
            )
        assert refused.value.code == "BUS_RECIPIENT_UNKNOWN"
        with pytest.raises(OperationRefused) as dispatch:
            await bus.handle_bus_dispatch(
                ctx, {"recipient": recipient, "objective": "x"}, _OPS["bus.dispatch"]
            )
        assert dispatch.value.code == "BUS_RECIPIENT_UNKNOWN"
    assert {m for m, _, _ in engine.engine.sent[sent:]} == {"BusRoster"}
    assert len(submitter.items) == 1


@pytest.mark.spec("ARBORA-OPS-R882")
async def test_dispatch_refuses_when_the_submitter_is_not_composed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _Bus(monkeypatch)
    with pytest.raises(OperationRefused) as refused:
        await bus.handle_bus_dispatch(
            _ctx(), {"recipient": "worker", "objective": "x"}, _OPS["bus.dispatch"]
        )
    assert refused.value.code == "UNAVAILABLE"
