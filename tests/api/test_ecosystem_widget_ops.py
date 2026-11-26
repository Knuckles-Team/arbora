"""Ecosystem widget reads (ARBORA-OPS-R914)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ecosystem
from arbora.gateway.models import WidgetData, WidgetField
from tests.api._engine_double import context

pytestmark = pytest.mark.spec("ARBORA-OPS-R914")


class FakeWidget:
    def __init__(self, name: str, down: dict[str, bool]) -> None:
        self.name, self.down = name, down

    def get_fields(self) -> list[WidgetField]:
        return [WidgetField(key="count", label=f"{self.name} count")]

    def fetch_data(self, config: Any) -> WidgetData:
        if self.down.get(self.name):
            return WidgetData(status="error", error="down")
        return WidgetData(fields={"count": len(self.name)}, status="ok")


class FakeRegistry:
    def __init__(self, names: list[str]) -> None:
        self.down: dict[str, bool] = {}
        self.names = names

    def list_all_known(self) -> list[str]:
        return self.names

    def get_widget(self, name: str) -> FakeWidget | None:
        return FakeWidget(name, self.down) if name in self.names else None


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> FakeRegistry:
    fake = FakeRegistry(["arr", "kafka", "ollama"])
    monkeypatch.setattr(ecosystem, "_widgets", lambda: fake)
    ecosystem._last_rows.clear()

    async def fleet(_ctx: Any, op_id: str, arguments: dict[str, Any]) -> Any:
        assert op_id == "fleet.status"
        return {"value": {"children": [{"name": "c1", "state": "up"}], "servers": []}}

    monkeypatch.setattr(ecosystem, "invoke_registered", fleet)
    return fake


async def test_each_widget_projects_rows_from_its_connector_call(
    registry: FakeRegistry,
) -> None:
    for name in ("arr", "kafka", "ollama"):
        reply = await ecosystem.handle_widget_read(
            context(),
            {"widget": name, "params": {}},
            None,  # type: ignore[arg-type]
        )
        assert reply["source"] == name and reply["degraded"] is False
        assert reply["rows"] == [
            {"key": "count", "label": f"{name} count", "value": len(name)}
        ]
        assert reply["fetched_at"]
    # services reads the children of the fleet status, never the servers.
    services = await ecosystem.handle_widget_read(
        context(),
        {"widget": "services", "params": {}},
        None,  # type: ignore[arg-type]
    )
    assert services["rows"] == [{"name": "c1", "state": "up"}]
    with pytest.raises(OperationRefused) as unknown:
        await ecosystem.handle_widget_read(
            context(),
            {"widget": "nope", "params": {}},
            None,  # type: ignore[arg-type]
        )
    assert unknown.value.code == "NOT_FOUND"


async def test_down_connector_gives_degraded_cached_rows(
    registry: FakeRegistry,
) -> None:
    ask: dict[str, Any] = {"widget": "arr", "params": {}}
    healthy = await ecosystem.handle_widget_read(context(), ask, None)  # type: ignore[arg-type]
    registry.down["arr"] = True
    degraded = await ecosystem.handle_widget_read(context(), ask, None)  # type: ignore[arg-type]
    assert degraded["degraded"] is True and degraded["rows"] == healthy["rows"]
    # Another tenant never sees this tenant's cached rows.
    other = await ecosystem.handle_widget_read(
        context("tenant:two"),
        ask,
        None,  # type: ignore[arg-type]
    )
    assert other["degraded"] is True and other["rows"] == []


def test_the_operation_is_registered_under_the_fleet_read_scope() -> None:
    (op,) = ecosystem.operations()
    assert op.id == "ecosystem.widgets.read" and op.scopes == frozenset({"fleet:read"})
    assert "services" in ecosystem.known_widgets()
