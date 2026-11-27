"""The host's owned caches and tool table (ARBORA-HOST-R039.5).

``HostCaches`` is the ``caches`` field of the host context. Each part owns its lock
and changes state only through its own methods; it is reached by argument and never
through a module accessor.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, MutableMapping
from dataclasses import dataclass, field
from typing import Any

__all__ = ["AnalysisCore", "HostCaches", "ToolTable", "WarmupSlot"]


class ToolTable(MutableMapping[str, Any]):
    """The registered tool functions of one host, by tool name.

    The REST twins and the in-process dispatch core resolve a tool here. The table
    also remembers the name set of a complete build so a headless caller can tell a
    full table from a partial one.
    """

    __slots__ = ("_complete", "_lock", "_tools")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[str, Any] = {}
        self._complete: frozenset[str] = frozenset()

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


@dataclass(frozen=True, slots=True)
class HostCaches:
    """Every owned cache of one host process."""

    tools: ToolTable = field(default_factory=ToolTable)
    warmup: WarmupSlot = field(default_factory=WarmupSlot)
    analysis: AnalysisCore = field(default_factory=AnalysisCore)
