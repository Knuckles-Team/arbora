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
