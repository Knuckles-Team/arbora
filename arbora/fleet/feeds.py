"""Typed events for the RUM, security-audit and CI/CD fleet feeds.

ARBORA-FLEET-R008 (.1 slice): a typed model per feed kind, each refusing an
unsanitized payload or a missing tenant boundary before it can become a
catalog item. Wiring these into live connector ingestion and the catalog's
``connector_items``/``_pack_annotations`` path is a further slice.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal

from arbora.fleet.catalog_items import PACK_ANNOTATION_FIELDS

FeedKind = Literal["rum", "security_audit", "cicd"]

#: Field names that must never reach a catalog item unredacted.
_SENSITIVE_KEYS = frozenset(
    {
        "password",
        "secret",
        "token",
        "authorization",
        "api_key",
        "apikey",
        "cookie",
        "set-cookie",
    }
)


class FeedEventRejected(ValueError):
    """A feed event failed sanitization or tenant-boundary validation."""


def _check_sanitized(payload: dict[str, Any]) -> None:
    for key in payload:
        if key.lower() in _SENSITIVE_KEYS:
            raise FeedEventRejected(f"unsanitized field {key!r} in feed payload")


@dataclass(frozen=True, slots=True)
class FeedEvent:
    """A single sanitized, tenant-scoped event from an approved fleet feed."""

    kind: FeedKind
    tenant: str
    source: str
    payload: dict[str, Any]

    def __post_init__(self) -> None:
        if not self.tenant or not self.tenant.strip():
            raise FeedEventRejected("feed event requires a non-empty tenant")
        if not self.source or not self.source.strip():
            raise FeedEventRejected("feed event requires a non-empty source")
        if not isinstance(self.payload, dict):
            raise FeedEventRejected("feed event payload must be a mapping")
        _check_sanitized(self.payload)


def rum_event(*, tenant: str, source: str, payload: dict[str, Any]) -> FeedEvent:
    """Build a real-user-monitoring feed event, refusing an unsanitized payload."""

    return FeedEvent(kind="rum", tenant=tenant, source=source, payload=payload)


def security_audit_event(
    *, tenant: str, source: str, payload: dict[str, Any]
) -> FeedEvent:
    """Build a security-audit feed event, refusing an unsanitized payload."""

    return FeedEvent(
        kind="security_audit", tenant=tenant, source=source, payload=payload
    )


def cicd_event(*, tenant: str, source: str, payload: dict[str, Any]) -> FeedEvent:
    """Build a CI/CD feed event, refusing an unsanitized payload."""

    return FeedEvent(kind="cicd", tenant=tenant, source=source, payload=payload)


_BUILDERS: Mapping[str, Callable[..., FeedEvent]] = MappingProxyType(
    {
        "rum": rum_event,
        "security_audit": security_audit_event,
        "cicd": cicd_event,
    }
)

#: Most recent events kept per intake; a bounded window, not a store. The
#: connector re-sends on reconnect, so nothing here is correctness state.
FEED_WINDOW = 1000


def _entry(event: FeedEvent) -> dict[str, Any]:
    digest = hashlib.sha256(
        json.dumps(event.payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    ident = hashlib.sha256(f"{event.tenant}\0{digest}".encode()).hexdigest()
    return {
        "pack": f"feed-{event.kind}",
        "name": f"{event.source}-{ident[:16]}",
        "op": f"feed.{event.kind}.event",
        "description": f"{event.kind} event from {event.source}",
        "tenant": event.tenant,
        "capability": f"feed.{event.kind}",
        "schema_digest": f"sha256:{digest}",
        "modality": "text",
        "cost": "low",
        "latency": "streaming",
    }


class FeedIntake:
    """The connector ingest job's entry into the catalog (ARBORA-FLEET-R008.2)."""

    def __init__(self, window: int = FEED_WINDOW) -> None:
        self._events: deque[FeedEvent] = deque(maxlen=window)

    def ingest(
        self, kind: str, *, tenant: str, source: str, payload: Mapping[str, Any]
    ) -> FeedEvent:
        """Accept one event or refuse with :class:`FeedEventRejected`."""
        builder = _BUILDERS.get(kind)
        if builder is None:
            raise FeedEventRejected(f"unknown feed kind {kind!r}")
        event = builder(tenant=tenant, source=source, payload=dict(payload))
        self._events.append(event)
        return event

    async def entries(self) -> tuple[Mapping[str, Any], ...]:
        """Accepted events as SDK pack entries for ``connector_items``."""
        unique = {(e.kind, e.tenant, _entry(e)["name"]): e for e in self._events}
        return tuple(_entry(e) for e in unique.values())


#: The host's one intake; ``bind_fleet_gateway`` serves it into the catalog.
FEED_INTAKE = FeedIntake()


def catalog_annotation_fields() -> tuple[str, ...]:
    """Annotation fields a feed event's catalog entry will need (ARBORA-FLEET-R004)."""

    return PACK_ANNOTATION_FIELDS


__all__ = [
    "FEED_INTAKE",
    "FeedEvent",
    "FeedIntake",
    "FeedEventRejected",
    "FeedKind",
    "catalog_annotation_fields",
    "cicd_event",
    "rum_event",
    "security_audit_event",
]
