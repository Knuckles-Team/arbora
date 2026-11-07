"""Bind the existing EG reader and multiplexer cache to fleet operations."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, MutableMapping
from typing import Any, Protocol, cast

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog
from arbora.fleet.catalog_sources import CombinedFleetSource, SdkRead
from arbora.fleet.multiplexer_ops import (
    CoverageEndpoints,
    CoverageRegistry,
    Invoke,
    Loadable,
    Mount,
    MultiplexerOps,
    NativeName,
    Notify,
    ReadItem,
    SessionKeyFor,
    UnadmittedServers,
)
from arbora.fleet.pin_store import PinStore
from arbora.fleet.session_loads import SessionLoads

logger = logging.getLogger(__name__)


class VerifiedReader(Protocol):
    async def read(self) -> Any: ...


class CachedProbe(Protocol):
    _probe_cache: MutableMapping[str, Mapping[str, Any]]

    def status_snapshot(self) -> Mapping[str, Any]: ...


async def _cached_protocol(mux: CachedProbe) -> Mapping[str, Mapping[str, Any]]:
    """Use completed probes only; discovery never starts a fleet sweep here."""
    return dict(mux._probe_cache)


#: A server whose probe failed or is never cached (an OAuth-gated child) is
#: not re-probed before this many seconds. A cached probe that is later
#: invalidated is re-probed on the next request. An explicit refresh of one
#: server runs at most once per window too.
PRIME_RETRY_S = 60.0
#: One server's priming probe may take at most this long.
PRIME_TIMEOUT_S = 20.0


def _good(info: Mapping[str, Any] | None) -> bool:
    return isinstance(info, Mapping) and info.get("error") is None


def _summary(info: Mapping[str, Any] | None) -> dict[str, Any]:
    """The caller-visible outcome of one refresh: a count, never the tools."""
    if not isinstance(info, Mapping):
        return {"tools": 0, "error": "refresh failed"}
    return {
        "tools": len(info.get("tools") or ()),
        "error": info.get("error"),
        "probed_at": info.get("probed_at"),
    }


class ProbePrimer:
    """Keep the probed half of the fleet catalog current.

    The verified EG catalog carries no input schema or tool annotations; the
    live probe does. Calling the primer probes only the servers a request
    names, so the served path never sweeps the fleet. A probe past the
    multiplexer's TTL is served while one background re-probe runs
    (ARBORA-FLEET-R070), and :meth:`refresh` re-reads a server at once, for a
    ``fleet.call`` of a tool the catalog does not know yet (ARBORA-FLEET-R071)
    and for the explicit refresh operation. A failed re-probe is logged and
    leaves the last good probe in place.
    """

    def __init__(
        self,
        mux: Any,
        *,
        retry_s: float = PRIME_RETRY_S,
        timeout_s: float = PRIME_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._mux = mux
        self._probe = getattr(mux, "probe_server", None)
        self._retry_s = retry_s
        self._timeout_s = timeout_s
        self._clock = clock
        self._attempted: dict[str, float] = {}
        self._refreshed: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._background: dict[str, asyncio.Task[Any]] = {}

    def _throttled(self, stamps: Mapping[str, float], server: str) -> bool:
        last = stamps.get(server)
        return last is not None and self._clock() - last < self._retry_s

    def _fresh(self, server: str) -> bool:
        if not _good(self._mux._probe_cache.get(server)):
            return False
        hit = getattr(self._mux, "_probe_cache_hit", None)
        return hit is None or hit(server) is not None

    def _keep_last_good(self, server: str, previous: Any) -> None:
        if _good(previous) and not _good(self._mux._probe_cache.get(server)):
            logger.warning("fleet re-probe of %s failed; keeping its catalog", server)
            self._mux._probe_cache[server] = previous

    async def _probe_once(self, server: str, *, force: bool) -> Any:
        """Probe one server; a good cached result clears its retry window."""
        probe = cast(Callable[..., Awaitable[Any]], self._probe)
        previous = self._mux._probe_cache.get(server)
        self._attempted[server] = self._clock()
        run = probe(server, force=True) if force else probe(server)
        try:
            info = await asyncio.wait_for(run, timeout=self._timeout_s)
        except Exception as exc:  # noqa: BLE001 - priming is best-effort
            logger.warning(
                "fleet probe of %s for catalog schema failed: %s",
                server,
                type(exc).__name__,
            )
            info = None
        self._keep_last_good(server, previous)
        if _good(self._mux._probe_cache.get(server)):
            # The window throttles failed and never-cached probes only.
            # A cached probe that a later schema refresh drops must be
            # re-primed on the next request, or its tools lose their
            # annotations and a read-only tool resolves as a write.
            self._attempted.pop(server, None)
        return info

    def _lock(self, server: str) -> asyncio.Lock:
        return self._locks.setdefault(server, asyncio.Lock())

    async def _reprobe_stale(self, server: str) -> None:
        async with self._lock(server):
            if not self._fresh(server):
                await self._probe_once(server, force=False)

    def _refresh_in_background(self, server: str) -> None:
        task = self._background.get(server)
        if (task is not None and not task.done()) or self._throttled(
            self._attempted, server
        ):
            return
        self._background[server] = asyncio.ensure_future(self._reprobe_stale(server))

    async def __call__(self, servers: Iterable[str]) -> None:
        if self._probe is None:
            return
        for server in _named(servers):
            if self._fresh(server):
                continue
            if _good(self._mux._probe_cache.get(server)):
                self._refresh_in_background(server)
                continue
            async with self._lock(server):
                if _good(self._mux._probe_cache.get(server)) or self._throttled(
                    self._attempted, server
                ):
                    continue
                await self._probe_once(server, force=False)

    async def refresh(self, servers: Iterable[str]) -> dict[str, dict[str, Any]]:
        """Re-probe each named server now, at most once per retry window."""
        outcome: dict[str, dict[str, Any]] = {}
        if self._probe is None:
            return outcome
        for server in _named(servers):
            async with self._lock(server):
                info: Any = self._mux._probe_cache.get(server)
                if not self._throttled(self._refreshed, server):
                    self._refreshed[server] = self._clock()
                    info = await self._probe_once(server, force=True)
                    info = self._mux._probe_cache.get(server, info)
            outcome[server] = _summary(info)
        return outcome


def _named(servers: Iterable[str]) -> list[str]:
    return [server for server in servers if isinstance(server, str) and server]


def probe_primer(
    mux: Any,
    *,
    retry_s: float = PRIME_RETRY_S,
    timeout_s: float = PRIME_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
) -> ProbePrimer:
    """The primer for one multiplexer; see :class:`ProbePrimer`."""
    return ProbePrimer(mux, retry_s=retry_s, timeout_s=timeout_s, clock=clock)


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
    pinned_cap: int | None = None,
    idle_ttl_seconds: int = 3600,
    pin_store: PinStore | None = None,
    coverage_registry: CoverageRegistry | None = None,
    coverage_endpoints: CoverageEndpoints | None = None,
    unadmitted_servers: UnadmittedServers | None = None,
) -> MultiplexerOps:
    """Build one operation service without claiming the MCP cutover is wired.

    ``reader`` is the process's already-bound ``DeferredFleetCatalogReader``.
    ``mux`` is the same process singleton used by the MCP server. The
    discovery/load-authorization policy owns ``visible``/``loadable``, and
    the native MCP tool registration and list-changed notification path owns
    the ``mount``/``notify`` hooks; supplying neither cannot silently widen
    authority. ``pinned_cap`` defaults to the ``FLEET_PINNED_CAP`` setting;
    ``pin_store`` is the durable per-principal pin store (without one the pin
    operations refuse ``UNAVAILABLE``).
    """

    async def live() -> Mapping[str, Mapping[str, Any]]:
        return await _cached_protocol(mux)

    prime = probe_primer(mux)

    def drop_probe(server: str) -> None:
        # ARBORA-FLEET-R040.3: a redeployed connector's cached probe is stale.
        mux._probe_cache.pop(server, None)

    source = CombinedFleetSource(
        verified=reader.read, live=live, sdk=sdk_entries, on_redeploy=drop_probe
    )
    catalog = FleetCatalog((source,), visible)

    def health() -> Mapping[str, Any]:
        return mux.status_snapshot().get("children", {})

    if pinned_cap is None:
        from arbora.core.settings import runtime_config

        pinned_cap = runtime_config.fleet_pinned_cap
    if unadmitted_servers is None:

        def unadmitted_servers():
            return list(mux.status_snapshot().get("unadmitted_servers", ()))

    return MultiplexerOps(
        catalog=catalog,
        sessions=SessionLoads(
            cap=cap, pinned_cap=pinned_cap, idle_ttl_seconds=idle_ttl_seconds
        ),
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
        refresh=prime.refresh,
        pin_store=pin_store,
        coverage_registry=coverage_registry,
        coverage_endpoints=coverage_endpoints,
        unadmitted_servers=unadmitted_servers,
    )
