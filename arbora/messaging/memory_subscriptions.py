"""Memory webhook subscriptions (ARBORA-MESSAGING-R700.1).

A subscription is ``{subscription_id, tenant, url, secret_ref, topics, active}``.
``secret_ref`` names an OpenBao path; arbora stores no secret value. Entries
persist in the engine KV namespace ``webhook_subscriptions`` under
``<tenant>/<subscription_id>``; ``send`` is the engine call of the caller
(``KvPut``, ``KvCas``, ``KvGet``, ``KvScan``, ``KvDelete``), so arbora holds no
store of its own.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

NAMESPACE = "webhook_subscriptions"
TOPICS = ("memory.derivation.completed", "memory.claim.changed", "memory.card.updated")

#: ``send(method, params, idempotency_key)`` returns the engine payload.
EngineSend = Callable[[str, dict[str, Any], str | None], Awaitable[Any]]


class InvalidSubscription(ValueError):
    """The request is invalid; ``field`` names the offending field."""

    def __init__(self, field: str) -> None:
        self.field = field
        super().__init__(field)


def _payload(result: Any) -> Any:
    return getattr(result, "payload", result)


def _encode(value: Mapping[str, Any]) -> bytes:
    return json.dumps(dict(value), sort_keys=True, separators=(",", ":")).encode()


def _decode(raw: Any) -> dict[str, Any]:
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            value = json.loads(bytes(raw))
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}
    return raw if isinstance(raw, dict) else {}


def validate(
    *, subscription_id: str, url: str, secret_ref: str, topics: list[str]
) -> None:
    """Raise ``InvalidSubscription`` naming the first invalid field."""
    if not subscription_id or "/" in subscription_id:
        raise InvalidSubscription("subscription_id")
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        raise InvalidSubscription("url")
    if not secret_ref:
        raise InvalidSubscription("secret_ref")
    if not topics or any(topic not in TOPICS for topic in topics):
        raise InvalidSubscription("topics")


class SubscriptionStore:
    """The tenant's subscriptions over the engine KV namespace."""

    def __init__(self, send: EngineSend) -> None:
        self._send = send

    @staticmethod
    def _key(tenant: str, subscription_id: str) -> str:
        return f"{tenant}/{subscription_id}"

    async def subscribe(
        self,
        tenant: str,
        *,
        subscription_id: str,
        url: str,
        secret_ref: str,
        topics: list[str],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        validate(
            subscription_id=subscription_id,
            url=url,
            secret_ref=secret_ref,
            topics=topics,
        )
        record = {
            "subscription_id": subscription_id,
            "tenant": tenant,
            "url": url,
            "secret_ref": secret_ref,
            "topics": sorted(set(topics)),
            "active": True,
        }
        key = self._key(tenant, subscription_id)
        current = _payload(
            await self._send("KvGet", {"namespace": NAMESPACE, "key": key}, None)
        )
        found = isinstance(current, Mapping) and "value" in current
        version = int(current.get("version", 0)) if found else 0
        # Compare-and-set on the version read, so a concurrent writer is refused.
        await self._send(
            "KvCas",
            {
                "namespace": NAMESPACE,
                "key": key,
                "expected_version": version,
                "new": _encode(record),
            },
            idempotency_key,
        )
        return record

    async def unsubscribe(
        self, tenant: str, subscription_id: str, *, idempotency_key: str | None = None
    ) -> bool:
        result = await self._send(
            "KvDelete",
            {"namespace": NAMESPACE, "key": self._key(tenant, subscription_id)},
            idempotency_key,
        )
        return bool(_payload(result))

    async def list(self, tenant: str) -> list[dict[str, Any]]:
        """Every subscription of ``tenant``; another tenant's are never read."""
        records: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            request: dict[str, Any] = {
                "namespace": NAMESPACE,
                "prefix": f"{tenant}/",
                "limit": 200,
            }
            if cursor:
                request["cursor"] = cursor
            page = _payload(await self._send("KvScan", request, None))
            rows = page.get("entries", []) if isinstance(page, Mapping) else []
            for row in rows:
                if isinstance(row, Mapping):
                    record = _decode(row.get("value"))
                    if record.get("tenant") == tenant:
                        records.append(record)
            cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
            if not cursor:
                return records

    async def active_for(self, tenant: str, topic: str) -> list[dict[str, Any]]:
        """Active subscriptions of ``tenant`` that listen to ``topic``."""
        return [
            record
            for record in await self.list(tenant)
            if record.get("active") and topic in record.get("topics", ())
        ]
