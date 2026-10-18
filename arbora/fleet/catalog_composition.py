"""Bind the existing EG reader and multiplexer cache to fleet operations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Mapping
from typing import Any, Protocol

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.catalog_sources import CombinedFleetSource, SdkRead
from arbora.fleet.multiplexer_ops import (
    Invoke,
    Loadable,
    Mount,
    MultiplexerOps,
    NativeName,
    Notify,
    ReadItem,
    SessionKeyFor,
)
from arbora.fleet.session_loads import SessionLoads

logger = logging.getLogger(__name__)


class VerifiedReader(Protocol):
    async def read(self) -> Any: ...


class CachedProbe(Protocol):
    _probe_cache: Mapping[str, Mapping[str, Any]]

    def status_snapshot(self) -> Mapping[str, Any]: ...


async def _cached_protocol(mux: CachedProbe) -> Mapping[str, Mapping[str, Any]]:
    """Use completed probes only; discovery never starts a fleet sweep here."""
    return dict(mux._probe_cache)


#: A primed server is not re-probed before this many seconds, even when its
#: probe failed or is never cached (an OAuth-gated child).
PRIME_RETRY_S = 60.0
#: One server's priming probe may take at most this long.
PRIME_TIMEOUT_S = 20.0


def probe_primer(
    mux: Any,
    *,
    retry_s: float = PRIME_RETRY_S,
    timeout_s: float = PRIME_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[Iterable[str]], Coroutine[Any, Any, None]]:
    """Probe named servers that have no cached protocol rows yet.

    The verified EG catalog carries no input schema or tool annotations; the
    live probe does. Probing only the servers a request names keeps the
    served path from ever sweeping the fleet. A failed probe is logged and
    leaves the catalog as it was.
    """
    attempted: dict[str, float] = {}
    locks: dict[str, asyncio.Lock] = {}
    probe = getattr(mux, "probe_server", None)

    async def prime(servers: Iterable[str]) -> None:
        if probe is None:
            return
        for server in servers:
            if not isinstance(server, str) or not server:
                continue
            if server in mux._probe_cache:
                continue
            lock = locks.setdefault(server, asyncio.Lock())
            async with lock:
                last = attempted.get(server)
                if server in mux._probe_cache or (
                    last is not None and clock() - last < retry_s
                ):
                    continue
                attempted[server] = clock()
                try:
                    await asyncio.wait_for(probe(server), timeout=timeout_s)
                except Exception as exc:  # noqa: BLE001 - priming is best-effort
                    logger.warning(
                        "fleet probe of %s for catalog schema failed: %s",
                        server,
                        type(exc).__name__,
                    )

    return prime


def compose_multiplexer_ops(
    *,
    reader: VerifiedReader,
    mux: CachedProbe,
    sdk_entries: SdkRead,
    visible: Callable[[CatalogItem, Any], Awaitable[bool]],
    loadable: Loadable,
    callable_item: Loadable,
    mount: Mount,
    notify: Notify,
    invoke: Invoke,
    session_key_for: SessionKeyFor,
    native_name: NativeName | None = None,
    read_item: ReadItem | None = None,
    cap: int = 64,
    idle_ttl_seconds: int = 3600,
) -> MultiplexerOps:
    """Build one operation service without claiming the MCP cutover is wired.

    ``reader`` is the process's already-bound ``DeferredFleetCatalogReader``.
    ``mux`` is the same process singleton used by the MCP server. The
    discovery/load-authorization policy owns ``visible``/``loadable``, and
    the native MCP tool registration and list-changed notification path owns
    the ``mount``/``notify`` hooks; supplying neither cannot silently widen
    authority.
    """

    async def live() -> Mapping[str, Mapping[str, Any]]:
        return await _cached_protocol(mux)

    prime = probe_primer(mux)
    source = CombinedFleetSource(verified=reader.read, live=live, sdk=sdk_entries)
    catalog = FleetCatalog((source,), visible)

    def health() -> Mapping[str, Any]:
        return mux.status_snapshot().get("children", {})

    return MultiplexerOps(
        catalog=catalog,
        sessions=SessionLoads(cap=cap, idle_ttl_seconds=idle_ttl_seconds),
        loadable=loadable,
        callable_item=callable_item,
        mount=mount,
        notify=notify,
        invoke=invoke,
        health=health,
        session_key_for=session_key_for,
        native_name=native_name,
        read_item=read_item,
        prime=prime,
    )
