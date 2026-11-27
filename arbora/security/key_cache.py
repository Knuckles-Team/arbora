"""The signing-key cache of one host (ARBORA-HOST-R039.5).

A ``JwksCache`` holds the JSON Web Key Sets a host fetched, by endpoint, for a
bounded time. It owns its lock and is reached by argument; no module holds one.
"""

from __future__ import annotations

import threading
from typing import Any

from arbora.core.settings import RuntimeConfig

__all__ = ["JWKS_TTL_SECONDS", "JwksCache"]

#: How long a fetched key set is served before it is fetched again.
JWKS_TTL_SECONDS = 300.0


class JwksCache:
    """Fetched key sets keyed by their endpoint, each valid until its expiry."""

    __slots__ = ("_lock", "_sets", "config")

    def __init__(self, config: RuntimeConfig) -> None:
        #: The configuration view of the host this cache serves: the verifiers that
        #: hold the cache read their security settings through it.
        self.config = config
        self._lock = threading.Lock()
        self._sets: dict[str, tuple[dict[str, Any], float]] = {}

    def get(self, endpoint: str, now: float) -> dict[str, Any] | None:
        """The key set cached for ``endpoint`` while it is unexpired."""
        with self._lock:
            entry = self._sets.get(endpoint)
        return entry[0] if entry is not None and now < entry[1] else None

    def put(self, endpoint: str, document: dict[str, Any], now: float) -> None:
        """Keep ``document`` for ``endpoint`` until ``now`` plus the TTL."""
        with self._lock:
            self._sets[endpoint] = (document, now + JWKS_TTL_SECONDS)
