"""Per-connector connection state read (ARBORA-FLEET-R089)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops.fleet import handle_connection_state, operations

pytestmark = pytest.mark.spec("ARBORA-FLEET-R089")

CHILDREN = {
    "beta": {
        "server": "beta",
        "state": "failed",
        "breaker": "open",
        "restart_count": 3,
        "sessions": 0,
        "in_flight": 0,
        "mounted_tools": 0,
        "catalog_refresh_error": "x",
    },
    "alpha": {
        "server": "alpha",
        "state": "ready",
        "breaker": "closed",
        "restart_count": 0,
        "sessions": 2,
        "in_flight": 1,
        "mounted_tools": 5,
    },
}


class Gateway:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def status(self, caller: Any, servers: tuple[str, ...] = ()) -> Any:
        self.calls.append(servers)
        children = {k: v for k, v in CHILDREN.items() if not servers or k in servers}
        return {"children": children, "coverage": {"ok": True}}


async def test_rows_carry_state_without_internals_or_credentials() -> None:
    gateway = Gateway()
    context = SimpleNamespace(
        caller=SimpleNamespace(), services={"fleet_gateway": gateway}
    )
    result = await handle_connection_state(context, {"servers": ()}, None)  # type: ignore[arg-type]
    assert result == {
        "connectors": [
            {
                "server": "alpha",
                "state": "ready",
                "breaker": "closed",
                "restart_count": 0,
                "mounted_tools": 5,
            },
            {
                "server": "beta",
                "state": "failed",
                "breaker": "open",
                "restart_count": 3,
                "mounted_tools": 0,
            },
        ]
    }
    only = await handle_connection_state(context, {"servers": ("beta",)}, None)  # type: ignore[arg-type]
    assert [row["server"] for row in only["connectors"]] == ["beta"]
    assert gateway.calls == [(), ("beta",)]


async def test_an_unbound_gateway_is_not_papered_over() -> None:
    context = SimpleNamespace(caller=SimpleNamespace(), services={})
    with pytest.raises(RuntimeError):
        await handle_connection_state(context, {"servers": ()}, None)  # type: ignore[arg-type]


def test_operation_is_a_discover_scoped_read() -> None:
    op = {o.id: o for o in operations()}["fleet.connections.state"]
    assert op.verb.value == "ask" and op.effect.value == "read"
    assert op.scopes == frozenset({"mcp:discover"})
