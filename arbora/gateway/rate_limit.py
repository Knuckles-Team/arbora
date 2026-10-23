"""Per-tenant token-bucket rate limiting for the API gateway.

CONCEPT:AU-OS.observability.no-op-without-metrics — Gateway Middle-Tier Hardening.

Pure-ASGI middleware mounted by
:func:`arbora.gateway.graph_api.register_graph_routes` INSIDE the
OS-5.14 :class:`~arbora.security.request_identity.ActorIdentityMiddleware`
so the server-minted :class:`~arbora.security.actor_context.ActorContext`
is already in scope. Bucket key precedence:

1. ``ActorContext.tenant_id`` (multi-tenant isolation)
2. authenticated ``actor_id`` (per-service/user when no tenant claim)
3. one shared anonymous bucket (no network identity collection)

The selected identity is immediately transformed with a per-process keyed hash;
raw tenant, actor, and network identifiers never become bucket keys, metric
labels, logs, or response fields.

Disabled by default (``GATEWAY_RATE_LIMIT=0``). The configured rate is the
sustained requests/second; ``GATEWAY_RATE_BURST`` is the bucket capacity
(default 2× rate). Rejections are ``429`` with a ``Retry-After`` header and
are counted on ``agent_utilities_gateway_rate_limited_total{tenant}``.

Scope note (documented, deliberate): buckets are in-memory and PER-PROCESS.
With ``GATEWAY_WORKERS=N`` or N replicas the effective limit is N× the
configured rate. Precise distributed limiting is a later
(state-externalization) concern — see ``docs/architecture/gateway_scaling.md``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import secrets
import threading
import time
from typing import Any

from arbora.observability.gateway_metrics import GATEWAY_RATE_LIMITED
from arbora.security.actor_context import current_actor
from arbora.security.request_identity import HEALTH_PATHS

logger = logging.getLogger(__name__)

# Liveness probes and Prometheus scrapes must never be throttled.
EXEMPT_PATHS: frozenset[str] = HEALTH_PATHS | {"/metrics"}

# Bucket-map hygiene: prune idle buckets so hostile/unbounded authenticated
# identity churn cannot grow the map without bound.
_PRUNE_THRESHOLD = 4096
_PRUNE_IDLE_SECONDS = 600.0


class _TokenBucket:
    """Classic token bucket: ``capacity`` tokens, refilled at ``rate``/sec."""

    __slots__ = ("capacity", "rate", "tokens", "updated")

    def __init__(self, rate: float, capacity: float, now: float) -> None:
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity  # new buckets start full → burst headroom
        self.updated = now

    def consume(self, now: float) -> tuple[bool, float]:
        """Take one token. Returns ``(allowed, retry_after_seconds)``."""
        elapsed = max(0.0, now - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        return False, (1.0 - self.tokens) / self.rate


class GatewayRateLimitMiddleware:
    """Pure-ASGI per-tenant token-bucket rate limiter
    (CONCEPT:AU-OS.observability.no-op-without-metrics)."""

    def __init__(
        self,
        app: Any,
        rate: float | None = None,
        burst: float | None = None,
    ) -> None:
        from arbora.core.settings import runtime_config as config

        self.app = app
        self.rate = float(rate if rate is not None else config.gateway_rate_limit or 0)
        raw_burst = float(
            burst if burst is not None else config.gateway_rate_burst or 0
        )
        # Default burst = 2× sustained rate; never below 1 token or the
        # very first request would be rejected.
        self.burst = raw_burst if raw_burst > 0 else max(self.rate * 2.0, 1.0)
        self._buckets: dict[str, _TokenBucket] = {}
        self._lock = threading.Lock()
        # Per-process HMAC-style key keeps client/tenant identifiers out of
        # memory keys, metric labels, and responses without creating a durable
        # cross-restart tracking identifier.
        self._privacy_key = secrets.token_bytes(32)

    # ------------------------------------------------------------------
    def _bucket_key(self) -> str:
        actor = current_actor()
        return self._identity_bucket_key(
            actor.tenant_id, actor.actor_id, actor.authenticated
        )

    def _identity_bucket_key(
        self, tenant: str | None, principal: str | None, authenticated: bool
    ) -> str:
        if tenant:
            raw = f"tenant:{tenant}"
        elif authenticated and principal:
            raw = f"actor:{principal}"
        else:
            raw = "anonymous"
        digest = hashlib.blake2s(
            raw.encode("utf-8"), key=self._privacy_key, digest_size=12
        ).hexdigest()
        return f"bucket_{digest}"

    def consume_verified_caller(self, caller: Any) -> tuple[bool, float]:
        """Use explicit verified operation authority, not ambient process identity."""
        from arbora.api.invoke.steps import VerifiedCaller

        if not isinstance(caller, VerifiedCaller) or not caller.authenticated:
            raise PermissionError("verified caller required for browser rate policy")
        if self.rate <= 0:
            return True, 0.0
        key = self._identity_bucket_key(caller.tenant, caller.principal, True)
        decision = self._consume(key)
        if not decision[0]:
            GATEWAY_RATE_LIMITED.labels(tenant=key).inc()
        return decision

    def _consume(self, key: str) -> tuple[bool, float]:
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= _PRUNE_THRESHOLD:
                    cutoff = now - _PRUNE_IDLE_SECONDS
                    self._buckets = {
                        k: b for k, b in self._buckets.items() if b.updated >= cutoff
                    }
                bucket = self._buckets[key] = _TokenBucket(self.rate, self.burst, now)
            return bucket.consume(now)

    # ------------------------------------------------------------------
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or self.rate <= 0
            or scope.get("path", "") in EXEMPT_PATHS
        ):
            await self.app(scope, receive, send)
            return

        key = self._bucket_key()
        allowed, retry_after = self._consume(key)
        if allowed:
            await self.app(scope, receive, send)
            return

        GATEWAY_RATE_LIMITED.labels(tenant=key).inc()
        retry = max(1, math.ceil(retry_after))
        body = json.dumps(
            {
                "error": "rate limit exceeded",
                "retry_after": retry,
            }
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", str(retry).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


__all__ = ["EXEMPT_PATHS", "GatewayRateLimitMiddleware"]
