"""A structural engine client double shared by the served-operation unit tests."""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import engine_calls as _engine
from arbora.core.settings import RuntimeConfig

Answer = Any | Callable[[Any], Any]


class EngineDouble:
    """Answers each engine method by name and records every send."""

    authenticated = True

    def __init__(self, **answers: Answer) -> None:
        self.answers = dict(answers)
        self.sent: list[tuple[str, Any, str, str | None]] = []
        self.claims: list[dict[str, Any]] = []

    def use_verified_context(self, claims: Any) -> Any:
        self.claims.append(dict(claims))
        return contextlib.nullcontext()

    async def _send(self, method: str, params: Any, graph: str, **keywords: Any) -> Any:
        self.sent.append((method, params, graph, keywords.get("idempotency_key")))
        answer = self.answers.get(method)
        if isinstance(answer, BaseException):
            raise answer
        return answer(params) if callable(answer) else answer


def fresh_config(config: RuntimeConfig | None) -> RuntimeConfig:
    """The given config, or a fresh one that shares no state."""
    return config if config is not None else RuntimeConfig()


def _services(
    config: RuntimeConfig | None, services: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "config": fresh_config(config),
        **(services if services is not None else {}),
    }


def context(
    client: EngineDouble,
    monkeypatch: pytest.MonkeyPatch,
    *,
    tenant: str = "tenant-a",
    services: dict[str, Any] | None = None,
    config: RuntimeConfig | None = None,
) -> Any:
    """A served-invocation context whose engine graph is ``tenant-content``."""

    async def graph(_context: Any) -> str:
        return "tenant-content"

    monkeypatch.setattr(_engine, "caller_dispatch_graph", graph)
    caller = SimpleNamespace(
        tenant=tenant, principal="alice", engine_claims={"principal": "alice"}
    )
    return SimpleNamespace(
        caller=caller,
        client=client,
        idempotency_key="key-1",
        services=_services(config, services),
    )
