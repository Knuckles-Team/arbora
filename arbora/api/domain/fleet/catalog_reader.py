"""Typed medulla fleet-catalog read and join seam.

``RegisterServer``-backed ``:Server`` rows in ``__commons__`` answer which
servers are live. ``AgentComponent.Search`` / ``Current`` / ``Content`` answer
what those servers provide. This module joins those two authorities without
an SQL, change-envelope, static-file, or live-probe fallback.

The protocol is Arbora's narrow application boundary over medulla's
generated ``AgentComponent`` and registered-server read contracts.  Concrete
wire DTOs remain owned by medulla and are translated only by the
composition-root adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final, Literal, Protocol

from arbora.api.domain.fleet.mcp_resource_reconciliation import (
    ReconciliationReceipt,
    require_reconciled_for_swap,
)
from arbora.core.environment import read_env
from arbora.pin_lag.unavailable import PinLagUnavailable

logger = logging.getLogger(__name__)

COMMONS_GRAPH: Final = "__commons__"
SERVER_QUERY_SOURCE: Final = "RegisterServer:ServerQuery"
COMPONENT_SEARCH_SOURCE: Final = "AgentComponent.Search"
COMPONENT_CURRENT_SOURCE: Final = "AgentComponent.Current"
COMPONENT_CONTENT_SOURCE: Final = "AgentComponent.Content"

ComponentKind = Literal["mcp_server", "tool", "skill", "mcp_prompt", "mcp_resource"]
FLEET_COMPONENT_KINDS: Final[tuple[ComponentKind, ...]] = (
    "mcp_server",
    "tool",
    "skill",
    "mcp_prompt",
    "mcp_resource",
)


class FleetCatalogIntegrityError(RuntimeError):
    """The two EG authorities could not be joined without ambiguity."""


@dataclass(frozen=True, slots=True)
class ReadContext:
    """Verified EG identity whose exact binding every read must carry."""

    tenant_id: str
    principal_id: str
    agent_id: str
    audience: str
    policy_version: str


@dataclass(frozen=True, slots=True)
class ReadReceipt:
    """Context and source provenance attached to one port response."""

    context: ReadContext
    source: str
    graph: str


@dataclass(frozen=True, slots=True)
class ServerRegistration:
    """One live ``RegisterServer``-owned ``:Server`` projection."""

    server_id: str
    name: str
    url: str
    resources: tuple[tuple[str, str], ...]
    registered_at_ms: int
    last_heartbeat_ms: int
    lease_expires_at_ms: int


@dataclass(frozen=True, slots=True)
class ServerPage:
    """One bounded page from the ``__commons__`` server query."""

    entries: tuple[ServerRegistration, ...]
    next_cursor: Any | None
    observed_at_ms: int
    total_live: int
    registry_revision: int
    registry_digest: str
    receipt: ReadReceipt


@dataclass(frozen=True, slots=True)
class ComponentPin:
    """Pinned identity used by MCP-origin component provenance."""

    component_id: str
    kind: ComponentKind
    definition_digest: str


class ComponentLifecycle(StrEnum):
    """Whether a catalog component entry is current (the engine's library lifecycle)."""

    PUBLISHED = "published"
    RETIRED = "retired"
    WITHDRAWN = "withdrawn"


@dataclass(frozen=True, slots=True)
class ComponentRecord:
    """Normalized current AgentComponent entry needed by fleet consumers."""

    component_id: str
    kind: ComponentKind
    server_name: str
    upstream_name: str
    summary: str
    tenant_id: str
    entry_revision: int
    definition_digest: str
    content_digest: str
    lifecycle: ComponentLifecycle
    provenance_server: ComponentPin | None
    #: A tool's MCP behaviour hints as the engine records them in its tool
    #: facts (``readOnlyHint`` and so on); ``None`` for other kinds
    #: (ARBORA-FLEET-R072).
    tool_hints: Mapping[str, bool] | None = None


@dataclass(frozen=True, slots=True)
class ComponentSearchRequest:
    """Exact shape arbora needs from pending kind-only EG search."""

    tenant_id: str
    kinds: tuple[ComponentKind, ...]
    limit: int
    cursor: Any | None = None
    #: Page one server's components, or one named component, without reading
    #: content (ARBORA-FLEET-R073). Needs the engine search filters; a pinned
    #: engine without them refuses ``UNAVAILABLE`` (``require_component_search_filters``).
    server_name: str | None = None
    upstream_name: str | None = None


@dataclass(frozen=True, slots=True)
class ComponentPage:
    """One bounded AgentComponent.Search page."""

    entries: tuple[ComponentRecord, ...]
    next_cursor: Any | None
    receipt: ReadReceipt


@dataclass(frozen=True, slots=True)
class CurrentComponent:
    """AgentComponent.Current result with its verified read receipt."""

    entry: ComponentRecord | None
    receipt: ReadReceipt


@dataclass(frozen=True, slots=True)
class ComponentContent:
    """AgentComponent.Content result pinned to one exact revision."""

    component_id: str
    entry_revision: int
    definition_digest: str
    content_digest: str
    media_type: str
    body: bytes
    receipt: ReadReceipt


@dataclass(frozen=True, slots=True)
class CatalogComponent:
    """A current component and its verified engine-owned content."""

    entry: ComponentRecord
    content: ComponentContent


@dataclass(frozen=True, slots=True)
class CatalogServer:
    """A current server component plus its optional live registration."""

    registration: ServerRegistration | None
    component: ComponentRecord
    content: ComponentContent
    provides: tuple[CatalogComponent, ...]


@dataclass(frozen=True, slots=True)
class CatalogGeneration:
    """The engine catalog generation a published catalog was read at.

    ``digest`` is the SHA-256 (64 lowercase hex characters) of the sorted item
    digests, so two replicas holding the same items agree on it.
    """

    tenant: str
    generation: int
    digest: str

    def __post_init__(self) -> None:
        if not self.tenant:
            raise ValueError("a catalog generation names its tenant")
        if isinstance(self.generation, bool) or self.generation < 0:
            raise ValueError("a catalog generation is a non-negative integer")
        if len(self.digest) != 64 or any(
            c not in "0123456789abcdef" for c in self.digest
        ):
            raise ValueError("a catalog digest is 64 lowercase hex characters")


def catalog_digest(components: Iterable[ComponentRecord]) -> str:
    """SHA-256 of the sorted per-item digests of ``components``."""
    items = sorted(
        hashlib.sha256(
            f"{c.component_id}\n{c.definition_digest}\n{c.content_digest}".encode()
        ).hexdigest()
        for c in components
    )
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FleetCatalog:
    """Tenant-bound joined view for loader, REST, and MCP adapters.

    ``unadmitted`` names live registrations without a current ``mcp_server``
    component. The catalog skips them. Onboarding admits them later.
    ``generation`` stamps the snapshot with the engine catalog generation.
    """

    context: ReadContext
    servers: tuple[CatalogServer, ...]
    unadmitted: tuple[str, ...] = ()
    generation: CatalogGeneration | None = None


class ReplicaState(StrEnum):
    """How far one replica's catalog generation is from the engine's."""

    CONVERGED = "converged"
    CONVERGING = "converging"
    DEGRADED = "degraded"


#: The reason a replica reports once it has been behind the engine too long.
CATALOG_GENERATION_BEHIND: Final = "catalog_generation_behind"
CONVERGENCE_TIMEOUT_KEY: Final = "ARBORA_REPLICA_CONVERGENCE_TIMEOUT_S"
CONVERGENCE_TIMEOUT_DEFAULT_S: Final = 30.0
CONVERGENCE_TIMEOUT_BOUNDS_S: Final = (1.0, 600.0)
RECEIPT_MAX_AGE_KEY: Final = "ARBORA_MCP_RESOURCES_RECEIPT_MAX_AGE_MS"
RECEIPT_MAX_AGE_DEFAULT_MS: Final = 300_000
RECEIPT_MAX_AGE_BOUNDS_MS: Final = (1_000, 3_600_000)


def _bounded_setting(key: str, default: float, bounds: tuple[float, float]) -> float:
    """A configured number inside ``bounds``; out of range is refused, not clamped."""
    value = float(read_env(key, default))
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low:g} and {high:g}")
    return value


def convergence_timeout_s() -> float:
    return _bounded_setting(
        CONVERGENCE_TIMEOUT_KEY,
        CONVERGENCE_TIMEOUT_DEFAULT_S,
        CONVERGENCE_TIMEOUT_BOUNDS_S,
    )


def receipt_max_age_ms() -> int:
    return int(
        _bounded_setting(
            RECEIPT_MAX_AGE_KEY,
            RECEIPT_MAX_AGE_DEFAULT_MS,
            RECEIPT_MAX_AGE_BOUNDS_MS,
        )
    )


@dataclass(frozen=True, slots=True)
class GenerationStatus:
    """One replica's active generation, the engine's, and the convergence state."""

    replica: str
    state: ReplicaState
    reason: str | None
    active: CatalogGeneration | None
    engine_generation: int


def generation_payload(status: GenerationStatus) -> dict[str, Any]:
    """The wire form of one replica's generation and convergence state."""

    active = status.active
    return {
        "replica": status.replica,
        "state": status.state.value,
        "reason": status.reason,
        "generation": None
        if active is None
        else {
            "tenant": active.tenant,
            "generation": active.generation,
            "digest": active.digest,
        },
        "engine_generation": status.engine_generation,
    }


class FleetCatalogReadPort(Protocol):
    """One long-lived EG client adapter used by ``FleetCatalogReader``.

    The eventual adapter owns one ``MedullaClient`` instance. Its
    server method queries only ``__commons__`` ``:Server`` rows written by
    ``RegisterServer``; the others map 1:1 to AgentComponent operations.
    """

    async def read_context(self) -> ReadContext: ...

    async def query_registered_servers(
        self, graph: str, _limit: int, cursor: Any | None, /
    ) -> ServerPage: ...

    async def search_components(
        self, _request: ComponentSearchRequest, /
    ) -> ComponentPage: ...

    async def current_component(
        self, *, tenant_id: str, component_id: str
    ) -> CurrentComponent: ...

    async def component_content(
        self, *, tenant_id: str, component_id: str, entry_revision: int
    ) -> ComponentContent: ...


class FleetCatalogSource(Protocol):
    """A composed authority capable of returning one verified fleet view."""

    async def read(self) -> FleetCatalog: ...


_MAX_CACHED_SNAPSHOTS: Final = 64
_MAX_CACHED_CONTENTS: Final = 50_000


#: A snapshot older than its TTL is still served, while one refresh runs, up
#: to this many TTLs old; past that a caller waits for the fresh read.
_STALE_SERVE_FACTOR: Final = 3.0


def _log_refresh_failure(task: asyncio.Task[Any]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("fleet catalog refresh failed: %s", type(exc).__name__)


class FleetCatalogReader:
    """Read and verify one complete Medulla-native fleet catalog snapshot."""

    def __init__(
        self,
        port: FleetCatalogReadPort,
        *,
        # The bound stops a non-terminating cursor, not a large fleet: one tool
        # per API operation puts a real fleet above 8,192 components, so the
        # default admits the validated maximum of 256 x 256 (ARBORA-FLEET-R026.1).
        page_size: int = 256,
        max_pages: int = 256,
        snapshot_ttl_s: float = 0.0,
        replica: str = "local",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if page_size < 1 or page_size > 256:
            raise ValueError("page_size must be between 1 and 256")
        if max_pages < 1 or max_pages > 256:
            raise ValueError("max_pages must be between 1 and 256")
        if snapshot_ttl_s < 0:
            raise ValueError("snapshot_ttl_s must not be negative")
        self._port = port
        self._page_size = page_size
        self._max_pages = max_pages
        # A full read is ~3 EG round trips per component (thousands on a real
        # fleet: ~1 min). One governed fleet.call admits its item several
        # times, so without reuse it outlived the caller's 300 s bearer. A
        # verified snapshot is reused per exact ReadContext (principal-bound)
        # for ``snapshot_ttl_s``; content is reused per immutable revision.
        self._snapshot_ttl_s = float(snapshot_ttl_s)
        self._cache_lock = threading.Lock()
        self._snapshots: dict[tuple[Any, ...], tuple[float, FleetCatalog]] = {}
        self._contents: dict[tuple[Any, ...], ComponentContent] = {}
        # One full read per key at a time, owned by the reader and shielded
        # from its callers (ARBORA-FLEET-R067): a caller whose own deadline
        # passes never cancels the read, so the snapshot always lands.
        self._inflight: dict[tuple[Any, ...], asyncio.Task[FleetCatalog]] = {}
        # The one published generation (ARBORA-FLEET-R051.2): replaced only by
        # ``publish_generation``, whole, so a read never sees two generations.
        self._replica = replica
        self._clock = clock
        self._published: FleetCatalog | None = None
        self._behind_since: float | None = None

    @property
    def published(self) -> FleetCatalog | None:
        """The last-known-good published catalog, or ``None`` before the first."""
        return self._published

    def publish_generation(
        self,
        candidate: FleetCatalog,
        receipt: ReconciliationReceipt | None,
        *,
        now_ms: int,
        max_age_ms: int | None = None,
    ) -> FleetCatalog:
        """Swap ``candidate`` in as the active generation, once the guard passes.

        The receipt is the engine's (ARBORA-MCP-RESOURCES-R002); nothing here
        issues one. A refusal raises ``PublicationRefusedError`` carrying the
        typed result and leaves the last-known-good generation active.
        """
        generation = candidate.generation
        if generation is None:
            raise FleetCatalogIntegrityError(
                "a candidate catalog carries no generation"
            )
        require_reconciled_for_swap(
            receipt,
            tenant_id=generation.tenant,
            expected_generation=generation.generation,
            expected_digest=generation.digest,
            now_ms=now_ms,
            max_age_ms=receipt_max_age_ms() if max_age_ms is None else max_age_ms,
        )
        with self._cache_lock:
            self._published = candidate
        return candidate

    async def fetch_receipt(self) -> ReconciliationReceipt | None:
        """The engine's reconciliation receipt for this tenant (ARBORA-FLEET-R051.2.2).

        PIN-LAG: a port whose engine adapter lacks the receipt method refuses
        ``UNAVAILABLE`` naming it.
        """
        fetch = getattr(self._port, "reconciliation_receipt", None)
        if fetch is None:
            raise PinLagUnavailable("ConnectorPack.catalog_reconciliation_receipt")
        return await fetch()

    async def publish_from_engine(
        self, candidate: FleetCatalog, *, now_ms: int
    ) -> FleetCatalog:
        """Fetch the engine receipt and swap ``candidate`` in through the guard."""
        return self.publish_generation(
            candidate, await self.fetch_receipt(), now_ms=now_ms
        )

    async def generation_status(self) -> GenerationStatus:
        """Compare the active generation with the engine's current one.

        A replica behind the engine for longer than the convergence timeout is
        ``degraded`` with reason ``catalog_generation_behind``; it keeps serving
        its last-known-good catalog and never merges two generations.
        """
        context = await self._port.read_context()
        self._validate_context(context)
        page = await self._port.query_registered_servers(COMMONS_GRAPH, 1, None)
        self._verify_receipt(
            page.receipt, context, source=SERVER_QUERY_SOURCE, graph=COMMONS_GRAPH
        )
        if page.registry_revision < 0:
            raise FleetCatalogIntegrityError("engine generation is negative")
        active = self._published.generation if self._published else None
        behind = active is None or active.generation < page.registry_revision
        now = self._clock()
        if not behind:
            self._behind_since = None
            return GenerationStatus(
                self._replica,
                ReplicaState.CONVERGED,
                None,
                active,
                page.registry_revision,
            )
        if self._behind_since is None:
            self._behind_since = now
        lagging = now - self._behind_since > convergence_timeout_s()
        return GenerationStatus(
            self._replica,
            ReplicaState.DEGRADED if lagging else ReplicaState.CONVERGING,
            CATALOG_GENERATION_BEHIND if lagging else None,
            active,
            page.registry_revision,
        )

    async def read(
        self,
        *,
        kinds: Sequence[ComponentKind] = FLEET_COMPONENT_KINDS,
    ) -> FleetCatalog:
        """Return a fully verified, deterministic live fleet snapshot."""
        selected_kinds = self._validated_kinds(kinds)
        context = await self._port.read_context()
        self._validate_context(context)
        key = (context, selected_kinds)
        if self._snapshot_ttl_s <= 0:
            return await self._read_full(context, selected_kinds)
        with self._cache_lock:
            cached = self._snapshots.get(key)
        age = None if cached is None else time.monotonic() - cached[0]
        if cached is not None and age is not None and age < self._snapshot_ttl_s:
            return cached[1]
        refresh = self._refresh(key, context, selected_kinds)
        if (
            cached is not None
            and age is not None
            and age < self._snapshot_ttl_s * _STALE_SERVE_FACTOR
        ):
            # Serve the last verified snapshot while one refresh runs: a full
            # read outlasts every interactive deadline on a real fleet.
            return cached[1]
        return await asyncio.shield(refresh)

    def _refresh(
        self,
        key: tuple[Any, ...],
        context: ReadContext,
        kinds: tuple[ComponentKind, ...],
    ) -> asyncio.Task[FleetCatalog]:
        """Start (or join) the one full read for ``key``; it caches on success."""

        task = self._inflight.get(key)
        if (
            task is not None
            and not task.done()
            and task.get_loop() is asyncio.get_running_loop()
        ):
            return task

        async def run() -> FleetCatalog:
            try:
                catalog = await self._read_full(context, kinds)
                with self._cache_lock:
                    if len(self._snapshots) >= _MAX_CACHED_SNAPSHOTS:
                        self._snapshots.clear()
                    self._snapshots[key] = (time.monotonic(), catalog)
                return catalog
            finally:
                if self._inflight.get(key) is task_ref[0]:
                    self._inflight.pop(key, None)

        task_ref: list[asyncio.Task[FleetCatalog]] = []
        task = asyncio.get_running_loop().create_task(run())
        task_ref.append(task)
        task.add_done_callback(_log_refresh_failure)
        self._inflight[key] = task
        return task

    async def _read_full(
        self, context: ReadContext, kinds: tuple[ComponentKind, ...]
    ) -> FleetCatalog:
        registrations, revision = await self._read_servers(context)
        components = await self._read_components(context, kinds)
        catalog = await self._join(context, registrations, components)
        return replace(
            catalog,
            generation=CatalogGeneration(
                tenant=context.tenant_id,
                generation=revision,
                digest=catalog_digest(components.values()),
            ),
        )

    @staticmethod
    def _validated_kinds(kinds: Sequence[ComponentKind]) -> tuple[ComponentKind, ...]:
        chosen = tuple(dict.fromkeys(kinds))
        if not chosen or "mcp_server" not in chosen:
            raise ValueError("fleet reads must include the mcp_server kind")
        if any(kind not in FLEET_COMPONENT_KINDS for kind in chosen):
            raise ValueError("fleet read contains an unsupported component kind")
        return chosen

    @staticmethod
    def _validate_context(context: ReadContext) -> None:
        claims = (
            context.tenant_id,
            context.principal_id,
            context.agent_id,
            context.audience,
            context.policy_version,
        )
        if any(not claim.strip() for claim in claims):
            raise FleetCatalogIntegrityError("verified read context is incomplete")

    @staticmethod
    def _verify_receipt(
        receipt: ReadReceipt,
        context: ReadContext,
        *,
        source: str,
        graph: str,
    ) -> None:
        if receipt != ReadReceipt(context=context, source=source, graph=graph):
            raise FleetCatalogIntegrityError(
                f"{source} response has mismatched context or provenance"
            )

    async def _read_servers(
        self, context: ReadContext
    ) -> tuple[dict[str, ServerRegistration], int]:
        servers: dict[str, ServerRegistration] = {}
        server_ids: set[str] = set()
        snapshot: tuple[int, int, str] | None = None
        async for page in self._server_pages():
            self._verify_receipt(
                page.receipt,
                context,
                source=SERVER_QUERY_SOURCE,
                graph=COMMONS_GRAPH,
            )
            identity = (
                page.total_live,
                page.registry_revision,
                page.registry_digest,
            )
            if snapshot is None:
                snapshot = identity
            elif identity != snapshot:
                raise FleetCatalogIntegrityError(
                    "registered-server snapshot changed during catalog read"
                )
            for server in page.entries:
                self._validate_server(server, observed_at_ms=page.observed_at_ms)
                if server.name in servers or server.server_id in server_ids:
                    raise FleetCatalogIntegrityError(
                        f"duplicate live server registration {server.name!r}"
                    )
                servers[server.name] = server
                server_ids.add(server.server_id)
        if snapshot is None or len(servers) != snapshot[0]:
            raise FleetCatalogIntegrityError(
                "registered-server pages did not exhaust the live snapshot"
            )
        return servers, snapshot[1]

    async def _read_components(
        self,
        context: ReadContext,
        kinds: tuple[ComponentKind, ...],
    ) -> dict[str, ComponentRecord]:
        components: dict[str, ComponentRecord] = {}
        async for page in self._component_pages(context, kinds):
            self._verify_receipt(
                page.receipt,
                context,
                source=COMPONENT_SEARCH_SOURCE,
                graph=context.tenant_id,
            )
            for entry in page.entries:
                self._validate_component(entry, context, kinds)
                if entry.component_id in components:
                    raise FleetCatalogIntegrityError(
                        f"duplicate component {entry.component_id!r}"
                    )
                current = await self._port.current_component(
                    tenant_id=context.tenant_id,
                    component_id=entry.component_id,
                )
                self._verify_receipt(
                    current.receipt,
                    context,
                    source=COMPONENT_CURRENT_SOURCE,
                    graph=context.tenant_id,
                )
                if current.entry != entry:
                    raise FleetCatalogIntegrityError(
                        f"component {entry.component_id!r} changed during catalog read"
                    )
                components[entry.component_id] = entry
        return components

    def _partition_components(
        self, components: dict[str, ComponentRecord]
    ) -> tuple[dict[str, ComponentRecord], dict[str, list[ComponentRecord]]]:
        server_components: dict[str, ComponentRecord] = {}
        children: dict[str, list[ComponentRecord]] = {}
        for component in components.values():
            if component.kind == "mcp_server":
                if component.server_name in server_components:
                    raise FleetCatalogIntegrityError(
                        f"multiple server components claim {component.server_name!r}"
                    )
                server_components[component.server_name] = component
                continue
            pin = component.provenance_server
            if pin is None:
                raise FleetCatalogIntegrityError(
                    f"component {component.component_id!r} lacks MCP provenance"
                )
            parent = components.get(pin.component_id)
            if parent is None or not self._pin_matches(pin, parent):
                raise FleetCatalogIntegrityError(
                    f"component {component.component_id!r} has an unresolved server pin"
                )
            if parent.server_name != component.server_name:
                raise FleetCatalogIntegrityError(
                    f"component {component.component_id!r} disagrees on server identity"
                )
            children.setdefault(parent.component_id, []).append(component)
        return server_components, children

    async def _join_server(
        self,
        context: ReadContext,
        registration: ServerRegistration | None,
        server_component: ComponentRecord,
        children: dict[str, list[ComponentRecord]],
    ) -> CatalogServer:
        server_content = await self._read_content(context, server_component)
        provided = []
        ordered_children = sorted(
            children.get(server_component.component_id, []),
            key=lambda item: (item.kind, item.upstream_name, item.component_id),
        )
        for child in ordered_children:
            provided.append(
                CatalogComponent(
                    entry=child,
                    content=await self._read_content(context, child),
                )
            )
        return CatalogServer(
            registration=registration,
            component=server_component,
            content=server_content,
            provides=tuple(provided),
        )

    async def _join(
        self,
        context: ReadContext,
        registrations: dict[str, ServerRegistration],
        components: dict[str, ComponentRecord],
    ) -> FleetCatalog:
        server_components, children = self._partition_components(components)
        unadmitted = tuple(sorted(set(registrations).difference(server_components)))
        if unadmitted:
            # A registration precedes its catalog import, so this is a normal
            # onboarding state, not tampering. Skip the server; never fail boot.
            logger.warning(
                "fleet catalog skipped %d live server(s) without a current "
                "mcp_server component: %s",
                len(unadmitted),
                ", ".join(unadmitted),
            )
        joined: list[CatalogServer] = []
        for name, server_component in server_components.items():
            joined.append(
                await self._join_server(
                    context, registrations.get(name), server_component, children
                )
            )
        joined.sort(key=lambda item: item.component.server_name)
        return FleetCatalog(
            context=context, servers=tuple(joined), unadmitted=unadmitted
        )

    async def _read_content(
        self, context: ReadContext, entry: ComponentRecord
    ) -> ComponentContent:
        key = (
            context,
            entry.component_id,
            entry.entry_revision,
            entry.definition_digest,
            entry.content_digest,
        )
        if self._snapshot_ttl_s > 0:
            with self._cache_lock:
                hit = self._contents.get(key)
            if hit is not None:
                return hit
        content = await self._port.component_content(
            tenant_id=context.tenant_id,
            component_id=entry.component_id,
            entry_revision=entry.entry_revision,
        )
        self._verify_receipt(
            content.receipt,
            context,
            source=COMPONENT_CONTENT_SOURCE,
            graph=context.tenant_id,
        )
        identity = (
            content.component_id == entry.component_id
            and content.entry_revision == entry.entry_revision
            and content.definition_digest == entry.definition_digest
            and content.content_digest == entry.content_digest
        )
        if not identity:
            raise FleetCatalogIntegrityError(
                f"content for {entry.component_id!r} does not match "
                "its current revision"
            )
        if self._snapshot_ttl_s > 0:
            with self._cache_lock:
                if len(self._contents) >= _MAX_CACHED_CONTENTS:
                    self._contents.clear()
                self._contents[key] = content
        return content

    async def _server_pages(self) -> AsyncIterator[ServerPage]:
        cursor: Any | None = None
        seen: set[str] = set()
        for _ in range(self._max_pages):
            page = await self._port.query_registered_servers(
                COMMONS_GRAPH, self._page_size, cursor
            )
            if (
                len(page.entries) > self._page_size
                or page.observed_at_ms <= 0
                or page.total_live < 0
                or page.registry_revision < 0
                or not self._is_digest(page.registry_digest)
            ):
                raise FleetCatalogIntegrityError("server query violated its page bound")
            yield page
            cursor = self._next_cursor(page.next_cursor, seen)
            if cursor is None:
                return
        raise FleetCatalogIntegrityError("server query exceeded its page bound")

    async def _component_pages(
        self, context: ReadContext, kinds: tuple[ComponentKind, ...]
    ) -> AsyncIterator[ComponentPage]:
        cursor: Any | None = None
        seen: set[str] = set()
        for _ in range(self._max_pages):
            page = await self._port.search_components(
                ComponentSearchRequest(
                    tenant_id=context.tenant_id,
                    kinds=kinds,
                    limit=self._page_size,
                    cursor=cursor,
                )
            )
            if len(page.entries) > self._page_size:
                raise FleetCatalogIntegrityError(
                    "component search violated its page bound"
                )
            yield page
            cursor = self._next_cursor(page.next_cursor, seen)
            if cursor is None:
                return
        raise FleetCatalogIntegrityError("component search exceeded its page bound")

    @staticmethod
    def _next_cursor(cursor: Any | None, seen: set[str]) -> Any | None:
        if cursor is None:
            return None
        if isinstance(cursor, str):
            key = cursor
        else:
            dump = getattr(cursor, "model_dump_json", None)
            if not callable(dump):
                raise FleetCatalogIntegrityError("page cursor is not contract-typed")
            key = dump()
        if not key or len(key.encode("utf-8")) > 4096 or key in seen:
            raise FleetCatalogIntegrityError(
                "page cursor is empty, oversized, or cyclic"
            )
        seen.add(key)
        return cursor

    @staticmethod
    def _validate_server(server: ServerRegistration, *, observed_at_ms: int) -> None:
        malformed = (
            not server.server_id.startswith("srv:")
            or server.server_id != f"srv:{server.name}"
            or not server.name
            or not server.url
            or server.registered_at_ms <= 0
            or server.last_heartbeat_ms < server.registered_at_ms
            or server.lease_expires_at_ms <= server.last_heartbeat_ms
            or server.lease_expires_at_ms <= observed_at_ms
        )
        if malformed:
            raise FleetCatalogIntegrityError("malformed live server registration")

    @staticmethod
    def _validate_component(
        entry: ComponentRecord,
        context: ReadContext,
        kinds: tuple[ComponentKind, ...],
    ) -> None:
        valid = all(
            (
                entry.tenant_id == context.tenant_id,
                entry.kind in kinds,
                entry.lifecycle == ComponentLifecycle.PUBLISHED,
                entry.entry_revision >= 1,
                bool(entry.component_id),
                bool(entry.server_name),
                FleetCatalogReader._is_digest(entry.definition_digest),
                FleetCatalogReader._is_digest(entry.content_digest),
            )
        )
        if not valid:
            raise FleetCatalogIntegrityError(
                f"component {entry.component_id!r} is malformed or out of scope"
            )
        if entry.kind == "mcp_server":
            if entry.provenance_server is not None:
                raise FleetCatalogIntegrityError(
                    f"server component {entry.component_id!r} cannot serve itself"
                )
        elif entry.provenance_server is None:
            raise FleetCatalogIntegrityError(
                f"component {entry.component_id!r} lacks MCP provenance"
            )

    @staticmethod
    def _pin_matches(pin: ComponentPin, entry: ComponentRecord) -> bool:
        return (
            pin.component_id == entry.component_id
            and pin.kind == "mcp_server"
            and entry.kind == "mcp_server"
            and pin.definition_digest == entry.definition_digest
        )

    @staticmethod
    def _is_digest(value: str) -> bool:
        prefix = "sha256:"
        if len(value) != len(prefix) + 64 or not value.startswith(prefix):
            return False
        digest = value[len(prefix) :]
        return all(character in "0123456789abcdef" for character in digest)


class DeferredFleetCatalogReader:
    """One startup-only binding cell for the process fleet authority.

    The MCP surface is constructed before the engine transport exists. Passing
    this reader into ``MCPMultiplexer`` disables its static-file path from the
    first instruction; startup later installs exactly one Medulla-backed reader and
    performs the initial refresh before readiness. Calls before installation
    fail closed rather than discovering another catalog.
    """

    def __init__(self) -> None:
        self._reader: FleetCatalogReader | None = None

    @property
    def installed(self) -> bool:
        return self._reader is not None

    def install(self, reader: FleetCatalogReader) -> None:
        if self._reader is not None:
            raise RuntimeError("fleet catalog authority is already installed")
        self._reader = reader

    async def read(
        self,
        *,
        kinds: Sequence[ComponentKind] = FLEET_COMPONENT_KINDS,
    ) -> FleetCatalog:
        if self._reader is None:
            # The typed, retryable refusal while the catalog warms (R078).
            from arbora.api.invoke.pipeline import OperationRefused
            from arbora.api.mcp.discovery import FLEET_RETRY_AFTER_S

            raise OperationRefused(
                "UNAVAILABLE",
                {"reason": "catalog warming", "retry_after_s": FLEET_RETRY_AFTER_S},
            )
        return await self._reader.read(kinds=kinds)

    async def publish_from_engine(
        self, candidate: FleetCatalog, *, now_ms: int
    ) -> FleetCatalog:
        if self._reader is None:
            raise RuntimeError("fleet catalog authority is not installed")
        return await self._reader.publish_from_engine(candidate, now_ms=now_ms)

    async def generation_status(self) -> GenerationStatus:
        if self._reader is None:
            from arbora.api.invoke.pipeline import OperationRefused
            from arbora.api.mcp.discovery import FLEET_RETRY_AFTER_S

            raise OperationRefused(
                "UNAVAILABLE",
                {"reason": "catalog warming", "retry_after_s": FLEET_RETRY_AFTER_S},
            )
        return await self._reader.generation_status()
