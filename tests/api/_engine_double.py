"""A structural engine double for operations that wait on an unreleased method.

The double declares the methods and request shapes a newer engine serves, so the
guard in ``arbora.pin_lag.medulla`` passes, and answers each from a handler. The
operation under test is the real one: only the engine behind it is replaced.
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

Handler = Callable[[dict[str, Any]], Any]


class EngineDouble:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.handlers: dict[str, Handler] = {}
        self.sent: list[tuple[str, dict[str, Any], bool]] = []
        self.tenant = ""

    def serve(self, method: str, handler: Handler, *models: str) -> None:
        import medulla.generated as generated
        from medulla.generated import models as generated_models

        self.handlers[method] = handler
        declared = frozenset(generated.METHOD_IDS) | frozenset(self.handlers)
        self.monkeypatch.setattr(generated, "METHOD_IDS", declared)
        for name in models:
            self.monkeypatch.setattr(generated_models, name, object, raising=False)

    def install(self, module: Any) -> None:
        async def send(
            context: Any,
            method: str,
            params: dict[str, Any],
            *,
            idempotent: bool = False,
        ) -> Any:
            self.sent.append((method, params, idempotent))
            self.tenant = str(context.caller.tenant)
            return self.handlers[method](params)

        self.monkeypatch.setattr(module, "engine_send", send)


def context(
    tenant: str = "tenant:one", scopes: frozenset[str] = frozenset(), key: str = "k"
) -> Any:
    caller = SimpleNamespace(
        tenant=tenant,
        effective_scopes=scopes,
        principal="principal:one",
        engine_claims={
            "agent_id": "agent:one",
            "principal": "principal:one",
            "tenant": tenant,
            "audience": "arbora",
            "policy_version": "p1",
            "scopes": [],
        },
    )
    return SimpleNamespace(
        caller=caller, idempotency_key=key, services={}, operation_services=None
    )
