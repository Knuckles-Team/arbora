"""A recording engine for the knowledge, curation and friction handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


def caller_context(*scopes: str) -> Any:
    caller = SimpleNamespace(
        tenant="tenant:one",
        principal="person:one",
        effective_scopes=frozenset(scopes),
    )
    return SimpleNamespace(caller=caller)


def record_engine(
    monkeypatch: pytest.MonkeyPatch, module: Any, reply: Any
) -> list[tuple[str, dict[str, Any], bool]]:
    """Replace the module's engine call; return what it was asked to send."""
    sent: list[tuple[str, dict[str, Any], bool]] = []

    async def engine_graph_op(
        context: Any, method: str, op: dict[str, Any], *, idempotent: bool = False
    ) -> Any:
        sent.append((method, op, idempotent))
        return reply

    monkeypatch.setattr(module, "engine_graph_op", engine_graph_op)
    return sent
