"""Shared fakes for tests exercising ``arbora.epistemic`` adapters directly.

``Actor`` is the one fake structurally satisfying
``arbora.epistemic.VerifiedActor`` that every direct-adapter test in this
tree authenticates as; duplicating the dataclass per module just lets the
fake drift from the real protocol it stands in for.
"""

from __future__ import annotations

import threading
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from typing import Any

from arbora.agent_runs import record_run_event
from arbora.mcp_server.served_engine import EngineHandles
from arbora.security.process_authority import ProcessAuthority
from arbora.security.secrets import SecretsBackend


@dataclass
class Actor:
    """A fake structurally satisfying ``arbora.epistemic.VerifiedActor``."""

    actor_id: str = "service:arbora"
    tenant_id: str = "tenant:a"
    roles: Iterable[str] = ("graph-client",)
    authenticated: bool = True
    current: bool = True

    def ensure_credential_current(self) -> None:
        if not self.current:
            raise PermissionError("expired")


class FakeSecretsBackend(SecretsBackend):
    """In-memory ``SecretsBackend`` with a genuinely atomic ``compare_and_set``."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._store.get(key)

    def set(self, key: str, value: str, **metadata) -> None:
        with self._lock:
            self._store[key] = value

    def delete(self, key: str) -> bool:
        with self._lock:
            return self._store.pop(key, None) is not None

    def list_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._store)

    def compare_and_set(self, key: str, expected: str, value: str, **metadata) -> bool:
        with self._lock:
            if self._store.get(key) != expected:
                return False
            self._store[key] = value
            return True


def unwrap_decide(params):
    """The ``request`` of one ``Decide``; the engine's wire params are ``{request}`` only."""
    assert list(params) == ["request"], "a Decide carries MethodDecideParams {request}"
    return params["request"]


def FixedEngineHandles(engine: object) -> EngineHandles:  # noqa: N802
    """An ``EngineHandles`` whose engine handle is one given object, for tests."""

    class _Fixed(EngineHandles):
        def served(self):  # type: ignore[override]
            return engine

    return _Fixed()


def run_events(engine: object):
    """The run-event sink of the harness, writing to one given engine."""
    return partial(record_run_event, engine_provider=lambda: engine)


def bound_authority(session: object) -> ProcessAuthority:
    """A process authority whose session slot holds one given session, for tests."""
    authority = ProcessAuthority()
    authority.bind_session(session)
    return authority


def served_host(invoke_operation: Any) -> Any:
    """A host whose gateway application answers served operations with the given call."""
    from types import SimpleNamespace

    application = SimpleNamespace(invoke_operation=invoke_operation)
    return SimpleNamespace(gateway=SimpleNamespace(application=lambda: application))
