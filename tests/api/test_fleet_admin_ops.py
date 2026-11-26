"""Fleet server, schedule, maintenance and code operations (ARBORA-OPS-R913)."""

from __future__ import annotations

import json
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
        rows: dict[str, dict[str, Any]] = {}

        def registered(params: dict[str, Any]) -> Any:
            rows[params["name"]] = {
                "name": params["name"],
                "url": params["url"],
                "desired": params["desired"],
                "ttl_secs": params["ttl_secs"],
                "transport": params.get("transport", "unspecified"),
                "resources": json.loads(params["resources_json"]),
            }
            return {}

        engine = EngineDouble(monkeypatch)
        engine.serve("RegisterServer", registered)
        engine.serve(
            "ListRegisteredServers",
            lambda params: {"entries": list(rows.values()), "next_cursor": None},
        )
        engine.install(surface)
        # A raw secret is refused by field path and the value is never echoed.
        with pytest.raises(OperationRefused) as raw:
            await surface.handle_server(
                context(),
                {"name": "x", "config": {"api_token": _RAW, "url": "u"}},
                register,
            )
        assert raw.value.code == "INVALID_ARGUMENT"
        assert _RAW not in repr(raw.value.details)
        assert not engine.sent
        await surface.handle_server(
            context(),
            {"name": "x", "config": {"api_token": "openbao://kv/x/token", "url": "u"}},
            register,
        )
        method, params, idempotent = engine.sent[0]
        assert method == "RegisterServer" and idempotent is True
        assert params["desired"] == "enabled" and params["url"] == "u"
        # The engine stores the reference and never a value.
        assert json.loads(params["resources_json"]) == {
            "api_token": "openbao://kv/x/token"
        }
        got = await surface.handle_server(
            context(), {"name": "x"}, _op("fleet.servers.config.get")
        )
        assert got["value"]["resources"]["api_token"] == "openbao://kv/x/token"
        rows["x"]["resources"] = {"api_token": _RAW}
        masked = await surface.handle_server(
            context(), {"name": "x"}, _op("fleet.servers.config.get")
        )
        assert masked["value"]["resources"]["api_token"] == "<redacted>"
        # Remove is disabled now; the engine sweep drops the row at lease expiry.
        await surface.handle_server(
            context(), {"name": "x"}, _op("fleet.servers.remove")
        )
        assert engine.sent[-1][1]["desired"] == "disabled"
        with pytest.raises(OperationRefused) as absent:
            await surface.handle_server(
                context(), {"name": "ghost"}, _op("fleet.servers.config.get")
            )
        assert absent.value.code == "NOT_FOUND"

    import asyncio

    asyncio.run(scenario())


async def test_a_pinned_engine_without_the_schedule_ticks_shape_refuses_naming_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(OperationRefused) as refused:
        await surface.handle_maintenance_status(
            context(), {}, _op("ops.maintenance.status")
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "JobOpScheduleTicks"


@pytest.mark.parametrize(
    ("op_id", "method", "params"),
    [
        (
            "schedule.tasks.read",
            "JobOpScheduleCalendar",
            {"from_": "2026-11-01T00:00:00Z", "to": "2026-11-02T00:00:00Z"},
        ),
        ("code.context.read", "CodeNavigation", {"action": "nav"}),
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

    def analytics(p: dict[str, Any]) -> Any:
        op = p["op"]
        if "ScheduleCalendar" in op:
            return {
                "entries": [
                    {"schedule_id": "s", "at_ms": op["ScheduleCalendar"]["from_ms"]}
                ],
                "logs": [],
            }
        if "ScheduleList" in op:
            return {
                "schedules": [
                    {"name": "maintenance", "state": "enabled", "task_kind": "maintain"}
                ]
            }
        return {"ticks": [{"outcome": "fired", "work_item_id": "w1"}]}

    engine.serve(
        "AnalyticsJob",
        analytics,
        "JobOpScheduleCalendar",
        "JobOpScheduleList",
        "JobOpScheduleTicks",
    )
    engine.serve(
        "ListWorkItems",
        lambda p: {
            "items": [
                {"work_item_id": "w1", "status": "running"},
                {"work_item_id": "w0", "status": "succeeded"},
            ]
        },
    )
    engine.serve("Maintain", lambda p: {"evicted": 0})
    engine.serve(
        "CodeNavigation", lambda p: {"items": [{"symbol": p["request"]["symbol"]}]}
    )
    engine.install(surface)

    async def graph(_context: Any) -> str:
        return "graph:one"

    monkeypatch.setattr("arbora.api.invoke.bootstrap.caller_dispatch_graph", graph)
    monkeypatch.setattr(surface, "_now_ms", lambda: 1_000)
    calendar = await surface.handle_engine(
        context(),
        {"from_": "1970-01-01T00:00:01Z", "to": "1970-01-01T00:00:02Z"},
        _op("schedule.tasks.read"),
    )
    assert calendar["entries"][0]["at_ms"] == 1_000
    assert engine.sent[0][1]["op"]["ScheduleCalendar"]["to_ms"] == 2_000
    with pytest.raises(OperationRefused) as bad:
        await surface.handle_engine(
            context(), {"from_": "soon", "to": "later"}, _op("schedule.tasks.read")
        )
    assert bad.value.code == "INVALID_ARGUMENT"
    status = await surface.handle_maintenance_status(
        context(), {}, _op("ops.maintenance.status")
    )
    assert status["value"]["state"] == "enabled" and status["value"]["pending"] == 1
    assert status["value"]["last_tick"]["work_item_id"] == "w1"
    assert "Maintain" not in [method for method, _p, _i in engine.sent]
    run = _op("ops.maintenance.run")
    assert run.effect is Effect.ADMIN and run.scopes == frozenset({"ops:admin"})
    await surface.handle_engine(context(), {"ids": ["n1"]}, run)
    sent = engine.sent[-1][1]
    assert sent["now_ms"] == 1_000 and sent["ids"] == ["n1"] and sent["delete"] is False
    code = await surface.handle_engine(
        context(), {"action": "nav", "symbol": "f"}, _op("code.context.read")
    )
    assert code["items"] == [{"symbol": "f"}]
    assert engine.sent[-1][1] == {"request": {"action": "nav", "symbol": "f"}}


def test_secret_masking_leaves_references_and_plain_fields() -> None:
    config = {"url": "u", "api_key": "env://K", "nested": [{"password": "p"}]}
    assert surface.masked(config) == {
        "url": "u",
        "api_key": "env://K",
        "nested": [{"password": "<redacted>"}],
    }
