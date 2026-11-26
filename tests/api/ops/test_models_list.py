"""models.list reads the engine's ModelProfile components (ARBORA-OPS-R878)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.steps import require_scopes
from arbora.api.ops import models
from arbora.api.registry import Effect

_OP = next(op for op in models.operations() if op.id == "models.list")


class _Graph:
    """The engine's model profiles by tenant, answering ``GetNodesByLabel`` as the caller."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.sent: list[tuple[str, dict[str, Any], str]] = []
        self.profiles: dict[str, list[list[Any]]] = {
            "tenant:one": [
                [f"profile:{i:03d}", {"display_name": f"P{i}", "provider": "vendor"}]
                for i in range(205)
            ],
            "tenant:two": [["profile:foreign", {"display_name": "F", "provider": "x"}]],
        }

        async def send(
            context: Any, method: str, params: dict[str, Any], **_: Any
        ) -> Any:
            tenant = context.caller.tenant
            self.sent.append((method, params, tenant))
            rows = self.profiles[tenant]
            after = params.get("after")
            if after is not None:
                rows = [r for r in rows if r[0] > after]
            return rows[: params["limit"]]

        monkeypatch.setattr(models, "engine_send", send)


def _context(tenant: str) -> Any:
    return SimpleNamespace(caller=SimpleNamespace(tenant=tenant), services={})


@pytest.mark.spec("ARBORA-OPS-R878")
def test_the_operation_is_a_kg_read_with_no_kind_parameter() -> None:
    assert _OP.effect is Effect.READ
    assert _OP.scopes == frozenset({"kg:read"})
    with pytest.raises(ValidationError):
        models.ModelsListParams(kind="chat")  # type: ignore[call-arg]
    with pytest.raises(ValidationError):
        models.ModelsListParams(limit=201)


@pytest.mark.spec("ARBORA-OPS-R878")
async def test_lists_only_visible_profiles_over_http_and_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(monkeypatch)
    first = await models.handle_models_list(_context("tenant:one"), {}, _OP)
    value = first["value"]
    assert len(value["models"]) == 200
    assert value["models"][0] == {
        "id": "profile:000",
        "display_name": "P0",
        "provider": "vendor",
    }
    assert value["next_cursor"] == "profile:199"
    second = await models.handle_models_list(
        _context("tenant:one"), {"cursor": value["next_cursor"]}, _OP
    )
    assert len(second["value"]["models"]) == 5
    assert second["value"]["next_cursor"] is None
    other = await models.handle_models_list(_context("tenant:two"), {}, _OP)
    assert [m["id"] for m in other["value"]["models"]] == ["profile:foreign"]
    assert all(row["id"] != "profile:foreign" for row in first["value"]["models"]), (
        "a foreign tenant's profile is absent"
    )
    assert {method for method, _, _ in graph.sent} == {"GetNodesByLabel"}
    assert graph.sent[0][1]["label"] == "ModelProfile"
    caller = SimpleNamespace(effective_scopes=frozenset({"registry:read"}))
    assert require_scopes(_OP, caller).code == "SCOPE_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R878")
def test_no_configuration_read_path_remains() -> None:
    import inspect

    source = inspect.getsource(models.handle_models_list)
    assert "runtime_config" not in source and "chat_models" not in source
