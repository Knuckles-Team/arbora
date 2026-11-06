"""The governed fleet path the modality handlers use (ARBORA-OPS-R899.2).

Handlers never open their own connector connection. They reach the fleet the
way the fleet agent does: the served ``find`` and ``act`` intent verbs, bound
in-process by :func:`arbora.api.mcp.serving.register_intent_surface`. Each call
therefore runs under the current verified session with the same admission,
scope and audit as an MCP client's ``act op=fleet.call``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any, Protocol

__all__ = ["FleetPort", "NativeIntentFleet", "catalog_items", "find_payload"]


class FleetPort(Protocol):
    async def search(self, query: str) -> list[dict[str, Any]]:
        """Catalog items matching ``query`` (``CatalogItem.public`` dicts)."""
        ...

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> Any:
        """Call one connector tool through ``fleet.call``; return its payload."""
        ...


class FleetCallFailed(RuntimeError):
    """The governed call answered with a refusal envelope."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _walk(value: Any) -> Iterator[Any]:
    yield value
    if isinstance(value, Mapping):
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)
    elif isinstance(value, str) and value[:1] in "{[":
        try:
            yield from _walk(json.loads(value))
        except ValueError:
            return


def catalog_items(envelope: Any) -> list[dict[str, Any]]:
    """Every catalog item dict in a ``fleet.catalog.search`` envelope."""
    items: list[dict[str, Any]] = []
    for node in _walk(envelope):
        if isinstance(node, Mapping) and isinstance(node.get("items"), list):
            items.extend(
                dict(item)
                for item in node["items"]
                if isinstance(item, Mapping) and item.get("name")
            )
    return items


def find_payload(envelope: Any, predicate: Any) -> dict[str, Any] | None:
    """The first mapping in ``envelope`` (JSON text included) matching ``predicate``."""
    for node in _walk(envelope):
        if isinstance(node, Mapping) and predicate(node):
            return dict(node)
    return None


def _refusal(envelope: Any) -> str | None:
    if isinstance(envelope, Mapping) and envelope.get("ok") is False:
        error = envelope.get("error")
        if isinstance(error, Mapping):
            return str(error.get("code") or "UNAVAILABLE")
        return "UNAVAILABLE"
    return None


class NativeIntentFleet:
    """:class:`FleetPort` over the in-process ``find``/``act`` intent verbs."""

    def __init__(self, verbs: Mapping[str, Any] | None = None) -> None:
        self._verbs = verbs

    def _verb(self, name: str) -> Any:
        verbs = self._verbs
        if verbs is None:
            from arbora.mcp_server import runtime

            verbs = runtime.NATIVE_INTENT_TOOLS
        verb = verbs.get(name)
        if verb is None:
            raise FleetCallFailed("UNAVAILABLE")
        return verb

    async def search(self, query: str) -> list[dict[str, Any]]:
        envelope = await self._verb("find")(
            op="fleet.catalog.search",
            params={"query": query, "kinds": ["tool"], "limit": 25},
        )
        code = _refusal(envelope)
        if code is not None:
            raise FleetCallFailed(code)
        return catalog_items(envelope)

    async def call(self, server: str, tool: str, arguments: Mapping[str, Any]) -> Any:
        envelope = await self._verb("act")(
            op="fleet.call",
            params={"server": server, "tool": tool, "arguments": dict(arguments)},
        )
        code = _refusal(envelope)
        if code is not None:
            raise FleetCallFailed(code)
        return envelope
