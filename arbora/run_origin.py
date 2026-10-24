"""Ephemeral host provenance for an awaited native agent tool call.

These objects are not delegation grants. They authorize only presentation of
an attended proposal under the unchanged admitted session.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from arbora.api.contracts.session import GraphSession, resolve_session


class NativeCallOrigin:
    """Shared revocable state; copied contexts cannot extend its lifetime."""

    def __init__(self, session: GraphSession, agent_name: str, run_id: str) -> None:
        self.session = session
        self.agent_name = agent_name
        self.run_id = run_id
        self._parent: NativeCallOrigin | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._callbacks: dict[
            object, tuple[asyncio.AbstractEventLoop, Callable[[], None]]
        ] = {}

    def is_live(self) -> bool:
        with self._lock:
            return not self._closed and (self._parent is None or self._parent.is_live())

    def subscribe_close(self, callback: Callable[[], None]) -> Callable[[], None]:
        loop = asyncio.get_running_loop()
        key = object()
        with self._lock:
            closed = self._closed
            if not closed:
                self._callbacks[key] = (loop, callback)
        if closed:
            self._notify(loop, callback)

        def unsubscribe() -> None:
            with self._lock:
                self._callbacks.pop(key, None)

        return unsubscribe

    @staticmethod
    def _notify(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> None:
        try:
            loop.call_soon_threadsafe(callback)
        except RuntimeError:
            # A stopped request loop has no executable proposal to withdraw.
            if not loop.is_closed():
                raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()
        for loop, callback in callbacks:
            self._notify(loop, callback)


_run: ContextVar[NativeCallOrigin | None] = ContextVar(
    "arbora_run_origin", default=None
)
_call: ContextVar[NativeCallOrigin | None] = ContextVar(
    "arbora_call_origin", default=None
)
_registry_lock = threading.RLock()
_runs: set[NativeCallOrigin] = set()


@contextmanager
def agent_run_origin_scope(
    session: GraphSession, agent_name: str, run_id: str | None
) -> Iterator[None]:
    """Bind explicit host run identity; unsupported sessions carry no origin."""
    origin = None
    if run_id:
        try:
            admitted = resolve_session(session)
        except PermissionError:
            pass
        else:
            origin = NativeCallOrigin(admitted, agent_name, run_id)
    token = _run.set(origin)
    if origin is not None:
        with _registry_lock:
            _runs.add(origin)
    try:
        yield
    finally:
        if origin is not None:
            origin.close()
            with _registry_lock:
                _runs.discard(origin)
        _run.reset(token)


@contextmanager
def native_call_origin_scope(run_id: str) -> Iterator[None]:
    parent = _run.get()
    origin = None

    def unsubscribe() -> None:
        pass

    if parent is not None and parent.run_id == run_id and parent.is_live():
        resolve_session(parent.session)
        origin = NativeCallOrigin(parent.session, parent.agent_name, run_id)
        origin._parent = parent
        unsubscribe = parent.subscribe_close(origin.close)
        if not parent.is_live():
            origin.close()
    token = _call.set(origin)
    try:
        yield
    finally:
        if origin is not None:
            origin.close()
        unsubscribe()
        _call.reset(token)


def require_native_call_origin() -> NativeCallOrigin:
    origin = _call.get()
    parent = _run.get()
    if origin is None or parent is None or not origin.is_live() or not parent.is_live():
        raise PermissionError("live native agent call origin is unavailable")
    resolve_session(origin.session)
    return origin


def revoke_run_origins(session: GraphSession, run_id: str) -> None:
    """Withdraw same-owner run proposals after successful durable cancellation."""
    admitted = resolve_session(session)
    with _registry_lock:
        matches = [
            origin
            for origin in _runs
            if origin.run_id == run_id
            and origin.session.tenant == admitted.tenant
            and origin.session.graph == admitted.graph
            and origin.session.actor.actor_id == admitted.actor.actor_id
        ]
    for origin in matches:
        origin.close()
