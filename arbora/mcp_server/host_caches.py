"""The host's owned caches and tool table (ARBORA-HOST-R039.5).

``HostCaches`` is the ``caches`` field of the host context. Each part owns its lock
and changes state only through its own methods; it is reached by argument and never
through a module accessor.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Iterator, Mapping, MutableMapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from arbora.core.settings import RuntimeConfig
from arbora.mcp_server.base_routes import BASE_ACTION_TOOL_ROUTES
from arbora.security.key_cache import JwksCache
from arbora.security.process_authority import ProcessAuthority

__all__ = [
    "AnalysisCore",
    "HostCaches",
    "IntentCache",
    "ToolTable",
    "WarmupSlot",
    "build_host_caches",
]


class ToolTable(MutableMapping[str, Any]):
    """The registered tool functions of one host, by tool name.

    The REST twins and the in-process dispatch core resolve a tool here. The table
    also remembers the name set of a complete build so a headless caller can tell a
    full table from a partial one.
    """

    __slots__ = (
        "_complete",
        "_lock",
        "_projection",
        "_routes",
        "_tools",
        "_verbs",
        "authority",
        "routes",
        "verbs",
    )

    def __init__(self, authority: ProcessAuthority) -> None:
        #: The process authority every dispatch through this table runs under
        #: (ARBORA-HOST-R039.9); it is the host's ``authority`` field.
        self.authority = authority
        self._lock = threading.RLock()
        self._tools: dict[str, Any] = {}
        self._complete: frozenset[str] = frozenset()
        self._verbs: dict[str, Any] = {}
        self._routes: dict[str, str] = dict(BASE_ACTION_TOOL_ROUTES)
        self._projection: Any | None = None
        #: The action-routed REST twin of each tool, a read-only view the registrars
        #: fill through :meth:`bind_route`; the route mount reads it.
        self.routes: Mapping[str, str] = MappingProxyType(self._routes)
        #: The served intent verbs (``find``, ``act``, ...) as in-process functions, a
        #: read-only view the serving composition fills through :meth:`bind_verb`. A
        #: run binds them as native tools; each call goes through the same
        #: invocation pipeline as an MCP client's.
        self.verbs: Mapping[str, Any] = MappingProxyType(self._verbs)

    def __getitem__(self, name: str) -> Any:
        with self._lock:
            return self._tools[name]

    def __setitem__(self, name: str, function: Any) -> None:
        with self._lock:
            self._tools[name] = function

    def __delitem__(self, name: str) -> None:
        with self._lock:
            del self._tools[name]

    def __iter__(self) -> Iterator[str]:
        with self._lock:
            return iter(tuple(self._tools))

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def bind_verb(self, name: str, function: Any) -> None:
        """Register one served intent verb under ``name``."""
        with self._lock:
            self._verbs[name] = function

    def bind_route(self, name: str, path: str) -> None:
        """Register the REST twin of tool ``name`` at ``path``."""
        with self._lock:
            self._routes[name] = path

    def bind_projection(self, projection: Any) -> None:
        """Make ``projection`` the served operation projection of this host."""
        with self._lock:
            self._projection = projection

    @property
    def projection(self) -> Any | None:
        """The served operation projection, or ``None`` before it is composed."""
        with self._lock:
            return self._projection

    def mark_complete(self) -> None:
        """Record the current names as the full set of a finished build."""
        with self._lock:
            self._complete = frozenset(self._tools)

    def is_complete(self) -> bool:
        """True when a finished build recorded names and every one is still present."""
        with self._lock:
            return bool(self._complete) and self._complete <= self._tools.keys()


class WarmupSlot:
    """The host's one catalog warm-up, once boot has started it."""

    __slots__ = ("_current", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._current: Any | None = None

    def bind(self, warmup: Any) -> None:
        """Make ``warmup`` the reported catalog state."""
        with self._lock:
            self._current = warmup

    def state(self) -> dict[str, Any] | None:
        """The warm-up state, or ``None`` before boot starts one."""
        with self._lock:
            current = self._current
        return None if current is None else current.state()


class AnalysisCore:
    """The shared analysis kernel handler the focused analyze tools delegate to."""

    __slots__ = ("_handler", "_lock")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handler: Any | None = None

    def bind(self, handler: Any) -> None:
        """Bind the handler that ``graph_analyze`` registration built."""
        with self._lock:
            self._handler = handler

    async def run(self, **arguments: Any) -> Any:
        """Run one canonical action; refuse when no analysis tool is registered."""
        with self._lock:
            handler = self._handler
        if handler is None:
            raise RuntimeError("analysis tools are not registered")
        return await handler(**arguments)


class _BoundedLRU:
    """A bounded least-recently-used map; the owner holds the lock."""

    __slots__ = ("_items", "_limit")

    def __init__(self, limit: int) -> None:
        self._items: OrderedDict[Any, Any] = OrderedDict()
        self._limit = limit

    def get(self, key: Any) -> Any | None:
        value = self._items.get(key)
        if value is not None:
            self._items.move_to_end(key)
        return value

    def put(self, key: Any, value: Any) -> None:
        self._items[key] = value
        self._items.move_to_end(key)
        while len(self._items) > self._limit:
            self._items.popitem(last=False)

    def drop_older_than(self, cutoff: float) -> None:
        """Drop entries whose stored ``(created_at, value)`` is older than ``cutoff``."""
        for key in [k for k, (born, _) in self._items.items() if born < cutoff]:
            del self._items[key]

    def clear(self) -> None:
        self._items.clear()


class IntentCache:
    """The intent router's state of one host.

    It holds the candidate table (with the generation that versions it), the ranked
    resolutions, the reviewed preview plans and the session approvals. Previews and
    approvals expire; every part is bounded.
    """

    __slots__ = (
        "_approvals",
        "_candidates",
        "_generation",
        "_lock",
        "_previews",
        "_resolutions",
    )

    ENTRY_LIMIT = 256
    TTL_SECONDS = 600.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._candidates: list[Any] | None = None
        self._generation = 0
        self._resolutions = _BoundedLRU(self.ENTRY_LIMIT)
        self._previews = _BoundedLRU(self.ENTRY_LIMIT)
        self._approvals = _BoundedLRU(self.ENTRY_LIMIT)

    def candidates(self) -> list[Any] | None:
        """The built candidate table, or ``None`` before the first build."""
        with self._lock:
            return self._candidates

    def store_candidates(self, built: list[Any]) -> None:
        """Keep a rebuilt candidate table and version it for the resolution key."""
        with self._lock:
            self._candidates = built
            self._generation += 1

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def resolution(self, key: tuple[Any, ...]) -> list[Any] | None:
        with self._lock:
            hit = self._resolutions.get(key)
        return None if hit is None else list(hit)

    def store_resolution(self, key: tuple[Any, ...], result: list[Any]) -> None:
        with self._lock:
            self._resolutions.put(key, result)

    def remember_preview(self, plan_ref: str, plan: Any, now: float) -> None:
        """Retain one reviewed plan until it expires."""
        with self._lock:
            self._previews.drop_older_than(now - self.TTL_SECONDS)
            self._previews.put(plan_ref, (now, plan))

    def preview(self, plan_ref: str, now: float) -> Any | None:
        """The reviewed plan under ``plan_ref`` while it is unexpired."""
        with self._lock:
            self._previews.drop_older_than(now - self.TTL_SECONDS)
            entry = self._previews.get(plan_ref)
        return None if entry is None else entry[1]

    def approve(self, key: str, now: float) -> None:
        with self._lock:
            self._approvals.drop_older_than(now - self.TTL_SECONDS)
            self._approvals.put(key, (now, True))

    def approved(self, key: str, now: float) -> bool:
        with self._lock:
            self._approvals.drop_older_than(now - self.TTL_SECONDS)
            return self._approvals.get(key) is not None


@dataclass(frozen=True, slots=True)
class HostCaches:
    """Every owned cache of one host process."""

    tools: ToolTable
    jwks: JwksCache
    warmup: WarmupSlot = field(default_factory=WarmupSlot)
    analysis: AnalysisCore = field(default_factory=AnalysisCore)
    intent: IntentCache = field(default_factory=IntentCache)


def build_host_caches(config: RuntimeConfig) -> HostCaches:
    """Fresh caches whose authority and key cache are bound to the host's config."""
    keys = JwksCache(config)
    return HostCaches(tools=ToolTable(ProcessAuthority(keys, config=config)), jwks=keys)
