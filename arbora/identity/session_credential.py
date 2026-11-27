"""The opaque browser session credential of the request being served (ARBORA-IDENTITY-R006.3).

The engine's second-factor operations act on the caller's own session, named by its
credential. A served operation has no credential of its own, so the browser admission
binds the verified request's credential here for the request's lifetime and the
operations read it. Nothing stores it: the binding ends with the request, and a surface
with no browser session (an MCP caller) binds none, so those operations refuse.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager

_CREDENTIAL: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "arbora_session_credential", default=None
)


@contextmanager
def bind_session_credential(credential: str) -> Iterator[None]:
    """Bind ``credential`` for the enclosed request only."""
    token = _CREDENTIAL.set(credential)
    try:
        yield
    finally:
        _CREDENTIAL.reset(token)


def session_credential() -> str | None:
    """The bound credential, or ``None`` when the request has no browser session."""
    return _CREDENTIAL.get()
