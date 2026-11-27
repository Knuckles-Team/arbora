"""Closed, versioned projections of the marketplace reads (ARBORA-DATA-MARKET-R003.4).

The provider answers a page or one record; Arbora projects it into the closed
version-1 shapes and nothing else. Every item keeps the provider's source, as-of,
session, freshness and provenance exactly as given: a missing value is an explicit
null or an ``unknown`` freshness, never a computed one. Decimals are strings, a
float is refused, and a provider-only key is dropped. A page cursor binds the
resolved tenant and account, the operation, the filters, the sort position and the
snapshot, so a cursor from another scope or filter set is refused.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused

SCHEMA_VERSION = 1
MAX_CURSOR_BYTES = 512
ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
INSTANT_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$"
DECIMAL_PATTERN = r"^-?\d+(\.\d+)?$"
UNIT_PATTERN = r"^[A-Za-z][A-Za-z0-9._-]{0,31}$"
CURRENCY_PATTERN = r"^[A-Z]{3}$"

Instant = Annotated[str, Field(pattern=INSTANT_PATTERN)]
Decimal = Annotated[str, Field(pattern=DECIMAL_PATTERN, max_length=64)]
Currency = Annotated[str, Field(pattern=CURRENCY_PATTERN)]
Unit = Annotated[str, Field(pattern=UNIT_PATTERN)]
ResourceId = Annotated[str, Field(pattern=ID_PATTERN)]


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Freshness(_Closed):
    state: Literal["fresh", "stale", "unknown"]
    age_seconds: int | None = Field(ge=0)


class Provenance(_Closed):
    record_id: str = Field(min_length=1, max_length=256)
    revision: str | None


class _Item(_Closed):
    """The metadata every item carries, as the provider gave it."""

    source: str = Field(min_length=1, max_length=128)
    as_of: Instant
    session: str | None
    freshness: Freshness
    provenance: Provenance


class Outcome(_Closed):
    name: str = Field(min_length=1, max_length=256)
    price: Decimal | None
    currency: Currency | None
    unit: Unit | None


class MarketItem(_Item):
    market_id: ResourceId
    event_id: ResourceId | None
    question: str = Field(max_length=2048)
    status: str = Field(min_length=1, max_length=32)
    starts_at: Instant | None
    ends_at: Instant | None
    outcomes: list[Outcome] = Field(max_length=256)
    updated_at: Instant | None


class EventItem(_Item):
    event_id: ResourceId
    title: str = Field(max_length=2048)
    description: str | None = Field(max_length=16384)
    status: str = Field(min_length=1, max_length=32)
    starts_at: Instant | None
    ends_at: Instant | None
    market_ids: list[ResourceId] = Field(max_length=1024)
    updated_at: Instant | None


class OrderItem(_Item):
    order_id: ResourceId
    market_id: ResourceId
    side: Literal["buy", "sell"]
    outcome: str | None = Field(max_length=256)
    price: Decimal | None
    price_currency: Currency | None
    price_unit: Unit | None
    quantity: Decimal | None
    quantity_unit: Unit | None
    status: str = Field(min_length=1, max_length=32)
    created_at: Instant | None
    updated_at: Instant | None


class PageInfo(_Closed):
    limit: int = Field(ge=1, le=100)
    next_cursor: str | None = Field(max_length=MAX_CURSOR_BYTES)


class _Page(_Closed):
    schema_version: Literal[1]
    as_of: Instant
    page: PageInfo


class MarketPage(_Page):
    items: list[MarketItem]


class EventPage(_Page):
    items: list[EventItem]


class OrderPage(_Page):
    items: list[OrderItem]


class _Get(_Closed):
    schema_version: Literal[1]
    as_of: Instant


class MarketGet(_Get):
    item: MarketItem


class EventGet(_Get):
    item: EventItem


class OrderGet(_Get):
    item: OrderItem


_ITEMS: dict[str, type[_Item]] = {
    "market": MarketItem,
    "event": EventItem,
    "order": OrderItem,
}
_PAGES: dict[str, type[_Page]] = {
    "market": MarketPage,
    "event": EventPage,
    "order": OrderPage,
}
_GETS: dict[str, type[_Get]] = {
    "market": MarketGet,
    "event": EventGet,
    "order": OrderGet,
}


def _malformed() -> OperationRefused:
    return OperationRefused("UNAVAILABLE", {"reason": "provider result malformed"})


def _keep(model: type[BaseModel], raw: Any) -> dict[str, Any]:
    """The provider record cut to the model's keys; a provider-only key is dropped."""
    if not isinstance(raw, Mapping):
        raise _malformed()
    fields = set(model.model_fields)
    return {key: raw[key] for key in fields if key in raw}


def _item(kind: str, raw: Any) -> dict[str, Any]:
    model = _ITEMS[kind]
    body = _keep(model, raw)
    for key in ("freshness", "provenance"):
        body[key] = _keep(
            Freshness if key == "freshness" else Provenance, body.get(key)
        )
    if kind == "market" and isinstance(body.get("outcomes"), list):
        body["outcomes"] = [_keep(Outcome, row) for row in body["outcomes"]]
    try:
        return model.model_validate(body).model_dump(mode="json")
    except ValidationError:
        raise _malformed() from None


def project_market(answer: Any) -> dict[str, Any]:
    return _project_get("market", answer)


def project_event(answer: Any) -> dict[str, Any]:
    return _project_get("event", answer)


def project_order(answer: Any) -> dict[str, Any]:
    return _project_get("order", answer)


def project_market_page(
    answer: Any, *, limit: int, cursor: str | None
) -> dict[str, Any]:
    return _project_page("market", answer, limit, cursor)


def project_event_page(
    answer: Any, *, limit: int, cursor: str | None
) -> dict[str, Any]:
    return _project_page("event", answer, limit, cursor)


def project_order_page(
    answer: Any, *, limit: int, cursor: str | None
) -> dict[str, Any]:
    return _project_page("order", answer, limit, cursor)


def _project_get(kind: str, answer: Any) -> dict[str, Any]:
    if not isinstance(answer, Mapping):
        raise _malformed()
    if answer.get("item") is None:
        # Absent and inaccessible look the same: no existence oracle.
        raise OperationRefused("NOT_FOUND")
    body = {
        "schema_version": SCHEMA_VERSION,
        "as_of": answer.get("as_of"),
        "item": _item(kind, answer["item"]),
    }
    try:
        return _GETS[kind].model_validate(body).model_dump(mode="json")
    except ValidationError:
        raise _malformed() from None


def _project_page(
    kind: str, answer: Any, limit: int, cursor: str | None
) -> dict[str, Any]:
    rows = answer.get("items") if isinstance(answer, Mapping) else None
    if not isinstance(rows, list) or len(rows) > limit:
        raise _malformed()
    body = {
        "schema_version": SCHEMA_VERSION,
        "as_of": answer.get("as_of"),
        "items": [_item(kind, row) for row in rows],
        "page": {"limit": limit, "next_cursor": cursor},
    }
    try:
        return _PAGES[kind].model_validate(body).model_dump(mode="json")
    except ValidationError:
        raise _malformed() from None


def filters_digest(filters: Mapping[str, Any]) -> str:
    """The stable digest of the filter set a cursor was issued under."""
    canonical = json.dumps(dict(filters), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


def encode_cursor(
    *,
    op: str,
    tenant: str,
    account: str,
    filters: Mapping[str, Any],
    position: str,
    snapshot: str,
) -> str:
    """The opaque cursor binding this scope, these filters, a sort position and a snapshot."""
    body = {
        "v": SCHEMA_VERSION,
        "op": op,
        "scope": hashlib.sha256(f"{tenant}\x00{account}".encode()).hexdigest()[:32],
        "filters": filters_digest(filters),
        "pos": position,
        "snap": snapshot,
    }
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    if len(token.encode()) > MAX_CURSOR_BYTES:
        raise _malformed()
    return token


def decode_cursor(
    token: str,
    *,
    op: str,
    tenant: str,
    account: str,
    filters: Mapping[str, Any],
) -> tuple[str, str]:
    """The ``(position, snapshot)`` of a cursor, or ``INVALID_ARGUMENT`` when it does not bind."""
    refused = OperationRefused("INVALID_ARGUMENT", {"field": "cursor"})
    if len(token.encode()) > MAX_CURSOR_BYTES:
        raise refused
    try:
        body = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    except (ValueError, binascii.Error):
        raise refused from None
    scope = hashlib.sha256(f"{tenant}\x00{account}".encode()).hexdigest()[:32]
    if (
        not isinstance(body, dict)
        or body.get("v") != SCHEMA_VERSION
        or body.get("op") != op
        or body.get("scope") != scope
        or body.get("filters") != filters_digest(filters)
        or not isinstance(body.get("pos"), str)
        or not isinstance(body.get("snap"), str)
    ):
        raise refused
    return body["pos"], body["snap"]


__all__ = [
    "EventGet",
    "EventItem",
    "EventPage",
    "MarketGet",
    "MarketItem",
    "MarketPage",
    "OrderGet",
    "OrderItem",
    "OrderPage",
    "decode_cursor",
    "encode_cursor",
    "project_event",
    "project_event_page",
    "project_market",
    "project_market_page",
    "project_order",
    "project_order_page",
]
