"""Fleet server, schedule, maintenance and code operations (ARBORA-OPS-R913)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import operator_surface as surface
from arbora.api.registry import Confirm, Effect
from tests.api._engine_double import EngineDouble, context

pytestmark = pytest.mark.spec("ARBORA-OPS-R913")

_OPS = {op.id: op for op in surface.operations()}
_NO_OP: Any = None
_RAW = "hunter2-raw-value"


def _op(op_id: str) -> Any:
    return _OPS[op_id]


def test_register_server_needs_plan_confirmation_and_hides_secret_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register = _op("fleet.servers.register")
    # An admin effect is confirmed in the console before it runs.
    assert register.effect is Effect.ADMIN and register.confirm is not Confirm.NONE
    assert register.principals.value == "human_undelegated"

    async def scenario() -> None:
        engine = EngineDouble(monkeypatch)
        engine.serve(
            "FleetServerRegistry",
            lambda params: {"name": "x", "config": {"api_token": _RAW, "url": "u"}},
        )
        engine.install()
        # A raw secret is refused by field path and the value is never echoed.
        with pytest.raises(OperationRefused) as raw:
            await surface.handle_engine(
                context(),
                {"name": "x", "config": {"api_token": _RAW}},
                register,
            )
        assert raw.value.code == "INVALID_ARGUMENT"
        assert _RAW not in repr(raw.value.details)
        assert not engine.sent
        reply = await surface.handle_engine(
            context(),
            {"name": "x", "config": {"api_token": "openbao://kv/x/token", "url": "u"}},
            register,
        )
        method, params, idempotent = engine.sent[0]
        assert method == "FleetServerRegistry" and idempotent is True
        assert params["op"]["operation"] == "register"
        # The engine answered a raw value: the reply masks it.
        assert _RAW not in repr(reply)
        got = await surface.handle_engine(
            context(), {"name": "x"}, _op("fleet.servers.config.get")
        )
        assert got["value"]["config"]["api_token"] == "<redacted>"
        assert engine.sent[1][1]["op"]["operation"] == "get_config"

    import asyncio

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("op_id", "method", "params"),
    [
        ("fleet.servers.register", "FleetServerRegistry", {"name": "x", "config": {}}),
        ("fleet.servers.update", "FleetServerRegistry", {"name": "x", "config": {}}),
        ("fleet.servers.remove", "FleetServerRegistry", {"name": "x"}),
        ("fleet.servers.config.get", "FleetServerRegistry", {"name": "x"}),
        (
            "schedule.tasks.read",
            "ScheduleCalendar",
            {"from_": "2026-11-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
        ),
        ("ops.maintenance.status", "MaintainStatus", {}),
        ("code.context.read", "CodeContext", {"action": "nav"}),
    ],
)
async def test_a_pinned_engine_without_the_method_refuses_naming_it(
    op_id: str, method: str, params: dict[str, Any]
) -> None:
    with pytest.raises(OperationRefused) as refused:
        await surface.handle_engine(context(), params, _op(op_id))
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == method


async def test_schedule_maintenance_and_code_read_through_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "ScheduleCalendar",
        lambda p: {
            "entries": [{"schedule_id": "s", "at_ms": p["from_ms"]}],
            "logs": [],
        },
    )
    engine.serve("MaintainStatus", lambda p: {"state": "idle", "pending": 0})
    engine.serve("Maintain", lambda p: {"evicted": 0})
    engine.serve("CodeContext", lambda p: {"items": [{"symbol": p["symbol"]}]})
    engine.install()
    monkeypatch.setattr(surface, "_now_ms", lambda: 1_000)
    calendar = await surface.handle_engine(
        context(),
        {"from_": "1970-01-01T00:00:01Z", "to": "1970-01-01T00:00:02Z"},
        _op("schedule.tasks.read"),
    )
    assert calendar["entries"][0]["at_ms"] == 1_000
    assert engine.sent[0][1] == {"from_ms": 1_000, "to_ms": 2_000}
    with pytest.raises(OperationRefused) as bad:
        await surface.handle_engine(
            context(), {"from_": "soon", "to": "later"}, _op("schedule.tasks.read")
        )
    assert bad.value.code == "INVALID_ARGUMENT"
    status = await surface.handle_engine(context(), {}, _op("ops.maintenance.status"))
    assert status["value"]["state"] == "idle"
    run = _op("ops.maintenance.run")
    assert run.effect is Effect.ADMIN and run.scopes == frozenset({"ops:admin"})
    await surface.handle_engine(context(), {"ids": ["n1"]}, run)
    sent = engine.sent[-1][1]
    assert sent["now_ms"] == 1_000 and sent["ids"] == ["n1"] and sent["delete"] is False
    code = await surface.handle_engine(
        context(), {"action": "nav", "symbol": "f"}, _op("code.context.read")
    )
    assert code["items"] == [{"symbol": "f"}]


def test_secret_masking_leaves_references_and_plain_fields() -> None:
    config = {"url": "u", "api_key": "env://K", "nested": [{"password": "p"}]}
    assert surface.masked(config) == {
        "url": "u",
        "api_key": "env://K",
        "nested": [{"password": "<redacted>"}],
    }
