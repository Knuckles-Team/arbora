"""Run-wide correlation for requests that cross agents and processes.

One correlation id ties every agent, span and side effect of a run together.
This module keeps that id, the session id and the W3C ``traceparent`` for the
current context, injects them into outbound headers, and restores them from an
inbound carrier. The resource priority header is carried through unchanged;
the knowledge engine decides what the priority means.
"""

from __future__ import annotations

import contextvars
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from agent_connector_sdk.identity import current_actor

__all__ = [
    "ACTOR_HEADER",
    "CORRELATION_HEADER",
    "PRIORITY_HEADER",
    "SESSION_HEADER",
    "TENANT_HEADER",
    "TRACEPARENT_HEADER",
    "bind_carrier",
    "current_carrier",
    "ensure_correlation_id",
    "extract",
    "get_correlation_id",
    "inject",
]

TRACEPARENT_HEADER = "traceparent"
CORRELATION_HEADER = "x-correlation-id"
SESSION_HEADER = "x-session-id"
TENANT_HEADER = "x-tenant-id"
ACTOR_HEADER = "x-actor-id"
PRIORITY_HEADER = "x-resource-priority"

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arbora_correlation_id", default=None
)
_session_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arbora_session_id", default=None
)
_priority: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arbora_resource_priority", default=None
)


def get_correlation_id() -> str | None:
    """Return the current run-wide correlation id, if any."""
    return _correlation_id.get()


def ensure_correlation_id() -> str:
    """Return the current correlation id, and create one when it is absent."""
    cid = _correlation_id.get()
    if cid:
        return cid
    cid = uuid.uuid4().hex
    _correlation_id.set(cid)
    return cid


def _traceparent(correlation_id: str) -> str:
    """Render a version-00 W3C ``traceparent`` derived from the correlation id."""
    trace = uuid.uuid5(uuid.NAMESPACE_OID, correlation_id).hex
    span = uuid.uuid5(uuid.NAMESPACE_OID, f"{correlation_id}:span").hex[:16]
    return f"00-{trace}-{span}-01"


def _context_headers() -> dict[str, str]:
    cid = ensure_correlation_id()
    headers = {CORRELATION_HEADER: cid, TRACEPARENT_HEADER: _traceparent(cid)}
    session = _session_id.get()
    if session:
        headers[SESSION_HEADER] = session
    priority = _priority.get()
    if priority:
        headers[PRIORITY_HEADER] = priority
    return headers


def current_carrier() -> dict[str, str]:
    """Serialize the active context for a spawned agent (see :func:`bind_carrier`)."""
    return _context_headers()


@contextmanager
def bind_carrier(carrier: dict[str, str] | None) -> Iterator[str]:
    """Restore a parent's context for the block and yield the correlation id."""
    carrier = carrier or {}
    cid = carrier.get(CORRELATION_HEADER) or ensure_correlation_id()
    tokens = [(_correlation_id, _correlation_id.set(cid))]
    for header, var in ((SESSION_HEADER, _session_id), (PRIORITY_HEADER, _priority)):
        value = carrier.get(header)
        if value:
            tokens.append((var, var.set(value)))
    try:
        yield cid
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def inject(headers: dict[str, str] | None = None) -> dict[str, str]:
    """Add the correlation and identity headers to an outbound header map."""
    merged = dict(headers or {})
    merged.update(_context_headers())
    try:
        actor = current_actor()
    except Exception:  # noqa: BLE001 - identity is optional context on a header
        return merged
    if actor.tenant_id:
        merged[TENANT_HEADER] = actor.tenant_id
    if actor.actor_id and actor.actor_id != "system":
        merged[ACTOR_HEADER] = actor.actor_id
    return merged


def extract(headers: dict[str, str] | None) -> dict[str, str]:
    """Pull the correlation carrier out of inbound headers (the inverse of inject)."""
    lower = {k.lower(): v for k, v in (headers or {}).items()}
    keys = (CORRELATION_HEADER, TRACEPARENT_HEADER, SESSION_HEADER, PRIORITY_HEADER)
    return {key: lower[key] for key in keys if key in lower}
