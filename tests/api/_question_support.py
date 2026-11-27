"""A ``Decide`` engine double that answers one question kind or refuses with a family code."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import question_round as _question
from tests.api._lane_double import EngineDouble, context

Q32 = 1 << 32
SCHEMA = {"component_id": "schema-1", "definition_digest": "d" * 64}


def answered(option: str) -> dict[str, Any]:
    """One advisory record whose single option is chosen."""
    scores = [{"option_id": option, "score": {"scale": "q32", "value": Q32}}]
    return {"records": [{"outcome": {"outcome": "advisory", "scores": scores}}]}


def served(
    monkeypatch: pytest.MonkeyPatch,
    *,
    decide: Any,
    scopes: tuple[str, ...] = (),
    services: dict[str, Any] | None = None,
) -> tuple[Any, EngineDouble]:
    """A served context whose engine answers ``Decide`` with ``decide`` and ``DecisionCommit`` with {}."""
    import medulla.generated as generated

    monkeypatch.setattr(_question, "require_question_kind", lambda _kind: None)

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any) -> Any:
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)
    engine = EngineDouble(Decide=decide, DecisionCommit={"committed": True})
    ctx = context(
        engine,
        monkeypatch,
        services={"task_plan_feature_schema": SCHEMA, **(services or {})},
    )
    ctx.caller = SimpleNamespace(
        **{**vars(ctx.caller), "effective_scopes": frozenset(scopes)}
    )
    return ctx, engine
