"""The served fleet gateway forwards every gateway method the fleet ops call (ARBORA-FLEET-R040.1)."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import arbora.api.ops.fleet as fleet_ops
from arbora.mcp_server.fleet_composition import LateFleetGateway


def _called_gateway_methods() -> set[str]:
    tree = ast.parse(Path(fleet_ops.__file__).read_text("utf-8"))
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "gateway"
    }


@pytest.mark.spec("ARBORA-FLEET-R040.1")
def test_late_gateway_serves_every_method_the_fleet_ops_call() -> None:
    called = _called_gateway_methods()
    assert {"prompts_list", "prompts_get", "resources_list", "resources_read"} <= called
    missing = sorted(name for name in called if not hasattr(LateFleetGateway, name))
    assert missing == []


@pytest.mark.spec("ARBORA-FLEET-R040.1")
@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("prompts_list", {"query": "triage"}),
        ("prompts_get", {"server": "s-mcp", "name": "p", "arguments": {}}),
        ("resources_list", {"query": ""}),
        ("resources_read", {"server": "s-mcp", "uri": "skill://x"}),
    ],
)
def test_late_gateway_forwards_relay_calls(method: str, params: dict[str, Any]) -> None:
    seen: list[tuple[str, Any, dict[str, Any]]] = []

    class _Inner:
        def __getattr__(self, name: str) -> Any:
            async def record(caller: Any, **kwargs: Any) -> Any:
                seen.append((name, caller, kwargs))
                return {"ok": name}

            return record

    late = LateFleetGateway()
    late.bind(_Inner(), ops=None)
    caller = SimpleNamespace(session=None)
    result = asyncio.run(getattr(late, method)(caller, **params))
    assert result == {"ok": method}
    assert seen == [(method, caller, params)]
