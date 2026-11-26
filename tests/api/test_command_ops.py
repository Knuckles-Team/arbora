"""Commands are exactly the served operations (ARBORA-OPS-R913)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import operator_surface as surface
from tests.api._served_support import confirmable  # noqa: F401
from tests.test_served_operation_api import _call, served  # noqa: F401

pytestmark = pytest.mark.spec("ARBORA-OPS-R913")


def _body(response: Any) -> dict[str, Any]:
    payload = response.json()
    result = payload.get("result", payload)
    return (
        result["value"] if isinstance(result, dict) and "value" in result else payload
    )


def test_commands_are_exactly_the_served_operations(served: Any) -> None:  # noqa: F811
    app, _client = served
    listed = _body(_call(app, "commands.autocomplete", {"prefix": ""}))
    commands = listed["commands"]
    # Every command is one registered operation id the caller's scopes admit.
    from arbora.api.ops.registry_factory import get_registry

    registry = get_registry()
    assert commands and all(registry.get(command) is not None for command in commands)
    assert "memory.list" in commands  # kv:read is held
    assert "ops.maintenance.run" not in commands  # ops:admin is not held
    assert not any(command.startswith("commands.") for command in commands)
    narrowed = _body(_call(app, "commands.autocomplete", {"prefix": "memory."}))
    assert narrowed["commands"] == [c for c in commands if c.startswith("memory.")]


def test_a_command_runs_under_the_target_scope_and_unknown_names_are_absent(
    confirmable: dict[str, Any],  # noqa: F811
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    # The caller lacks ops:admin: the name grants nothing the operation refuses.
    refused = _call(
        app,
        "commands.execute",
        {"command": "ops.maintenance.run", "args": {}},
        **{"Idempotency-Key": "k-1"},
    )
    assert refused.json()["error"]["code"] in {"SCOPE_REQUIRED", "NOT_FOUND"}
    absent = _call(
        app,
        "commands.execute",
        {"command": "no.such.command", "args": {}},
        **{"Idempotency-Key": "k-2"},
    )
    assert absent.json()["error"]["code"] == "NOT_FOUND"
    nested = _call(
        app,
        "commands.execute",
        {"command": "commands.autocomplete", "args": {}},
        **{"Idempotency-Key": "k-3"},
    )
    assert nested.json()["error"]["code"] == "NOT_FOUND"
    assert "commands.execute" in surface.table_ids()


def test_a_command_in_scope_runs_the_operation_itself(
    confirmable: dict[str, Any],  # noqa: F811
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    direct = _call(app, "memory.list", {})
    ran = _call(
        app,
        "commands.execute",
        {"command": "memory.list", "args": {}},
        **{"Idempotency-Key": "k-4"},
    )
    assert ran.status_code == direct.status_code, ran.text
    assert "error" not in ran.json() or ran.json()["error"]["code"] == (
        direct.json().get("error", {}).get("code")
    )
    if direct.status_code == 200:
        inner = _body(ran)["result"]
        assert (inner.get("value", inner)) == (
            _body(direct).get("value", _body(direct))
        )
