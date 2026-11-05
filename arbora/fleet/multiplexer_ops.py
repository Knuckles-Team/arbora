"""Registry-facing dynamic fleet operations; MCP wiring is a separate cutover."""

from __future__ import annotations

import builtins
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Any, cast

from arbora.fleet.catalog_items import CatalogItem, FleetCatalog, ItemKind
from arbora.fleet.pin_store import PinRecord, PinStore, pinned_now_ms
from arbora.fleet.session_loads import LoadCapExceeded, SessionLoads

Loadable = Callable[[CatalogItem, Any], Awaitable[bool]]
Mount = Callable[[CatalogItem, Callable[..., Awaitable[Any]] | None], Awaitable[None]]
Notify = Callable[[str], Awaitable[bool]]
Invoke = Callable[[str, Mapping[str, Any], Any, str], Awaitable[Any]]
Health = Callable[[], Mapping[str, Any]]
SessionKeyFor = Callable[[Any], str | None]
NativeName = Callable[[CatalogItem], str]
ReadItem = Callable[[CatalogItem, Mapping[str, Any], Any], Awaitable[Any]]
#: Fill the live protocol rows (input schema, annotations) of named servers.
Prime = Callable[[Iterable[str]], Awaitable[None]]
#: The coverage registry reader and endpoint lister for ``status``.
CoverageRegistry = Callable[[], tuple[Any, ...]]
CoverageEndpoints = Callable[[], tuple[Any, ...]]
UnadmittedServers = Callable[[], list[str]]

#: A server-filtered search primes at most this many servers.
_MAX_PRIMED_SERVERS = 8
#: One pin or unpin request names at most this many items (ARBORA-FLEET-R059.4).
_MAX_PINNED_ITEMS = 32


def _loaded_ids(loaded: Any) -> list[str]:
    """The bounded item-id list a load-cap refusal payload carries."""
    return [
        row["id"]
        for row in loaded
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    ]


def _load_cap_refusal(exc: LoadCapExceeded) -> Any:
    """The stable refusal a session/pinned cap overflow becomes."""
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused(
        "LOAD_CAP_EXCEEDED",
        {"cap": exc.cap, "loaded_items": _loaded_ids(exc.loaded)},
    )


def _default_coverage_registry() -> tuple[Any, ...]:
    """The org registry: the ``connectors`` group of ``workspace.yml``."""
    from arbora.fleet.coverage import registered_connectors
    from arbora.fleet.onboarding import self_served_connectors

    return registered_connectors(exclude=self_served_connectors())


def _default_coverage_endpoints() -> tuple[Any, ...]:
    """The deployed MCP config's enabled endpoints, self-served excluded."""
    from arbora.fleet.onboarding import (
        configured_fleet_endpoints,
        self_served_connectors,
    )

    return configured_fleet_endpoints(exclude=self_served_connectors())


async def _no_prime(_servers: Iterable[str]) -> None:
    return None


def item_binding(item: CatalogItem) -> tuple[str, ...]:
    """Bind a loaded grant to catalog identity and the exact upstream target."""
    return (item.id, item.kind, item.server or "", item.name)


def _require_delegate_scope(caller: Any) -> None:
    if "mcp:delegate" not in caller.effective_scopes:
        raise PermissionError("mcp:delegate is required")


def _require_discover_scope(caller: Any) -> None:
    if "mcp:discover" not in caller.effective_scopes:
        raise PermissionError("mcp:discover is required")


#: Catalog kinds that each relayed MCP list family covers.
RELAY_KINDS: dict[str, tuple[ItemKind, ...]] = {
    "prompts": ("prompt",),
    "resources": ("resource", "resource_template", "skill"),
}
_SKILL_SCHEME = "skill://"
_SKILL_MAIN = "/SKILL.md"


def resource_uri(kind: str, name: str) -> str:
    """The MCP resource URI a catalog resource or skill item is read by."""
    if kind == "skill":
        return f"{_SKILL_SCHEME}{name}{_SKILL_MAIN}"
    return name


def _surface_item_id(family: str, server: str, ident: str) -> str:
    if family == "prompts":
        return f"fleet:prompt:{server}/{ident}"
    if ident.startswith(_SKILL_SCHEME) and ident.endswith(_SKILL_MAIN):
        name = ident[len(_SKILL_SCHEME) : -len(_SKILL_MAIN)]
        return f"fleet:skill:{server}/{name}"
    return f"fleet:resource:{server}/{ident}"


class MultiplexerOps:
    """Discover, mount and track items under injected caller authority.

    The native tool body is only an ``invoke('fleet.call')`` wrapper. The final
    MCP cutover owns registration and session-visibility middleware; it must
    use :meth:`dispatchable` for both tools/list and tools/call.
    """

    def __init__(
        self,
        *,
        catalog: FleetCatalog,
        sessions: SessionLoads,
        loadable: Loadable,
        mount: Mount,
        notify: Notify,
        invoke: Invoke,
        health: Health,
        session_key_for: SessionKeyFor | None = None,
        callable_item: Loadable | None = None,
        native_name: NativeName | None = None,
        read_item: ReadItem | None = None,
        prime: Prime | None = None,
        pin_store: PinStore | None = None,
        coverage_registry: CoverageRegistry | None = None,
        coverage_endpoints: CoverageEndpoints | None = None,
        unadmitted_servers: UnadmittedServers | None = None,
    ) -> None:
        self.catalog = catalog
        self._prime = prime or _no_prime
        self.sessions = sessions
        self._loadable = loadable
        self._mount = mount
        self._notify = notify
        self._invoke = invoke
        self._health = health
        self._session_key_for = session_key_for or (lambda _caller: None)
        self._callable_item = callable_item

        self._native_name = native_name or (lambda item: item.id)
        self._native_ids: dict[str, str] = {}
        self._read_item = read_item
        #: The durable per-principal pin store; without one, pin operations
        #: refuse ``UNAVAILABLE`` (ARBORA-FLEET-R059).
        self._pin_store = pin_store
        self._coverage_registry = coverage_registry or _default_coverage_registry
        self._coverage_endpoints = coverage_endpoints or _default_coverage_endpoints
        self._unadmitted_servers = unadmitted_servers or (lambda: [])

    async def admitted_tool(self, caller: Any, server: str, tool: str) -> CatalogItem:
        """Return one call-authorized descriptor without requiring discovery scope."""
        _require_delegate_scope(caller)
        # The verified EG record carries no schema or annotations; the
        # target's live probe does. Without it a read-only tool resolves as
        # a write, and the caller never learns the tool's parameters.
        await self._prime((server,))
        item = await self.catalog._raw_get(f"fleet:tool:{server}/{tool}")
        if (
            item is None
            or item.kind != "tool"
            or item.server != server
            or item.name != tool
        ):
            raise PermissionError("fleet tool is unavailable")
        if not item.required_scopes.issubset(caller.effective_scopes):
            raise PermissionError("child scopes are required")
        if (
            self._callable_item is None
            or await self._callable_item(item, caller) is not True
        ):
            raise PermissionError("fleet call denied by policy")
        return item

    async def admitted_surface_item(
        self, caller: Any, family: str, server: str, ident: str
    ) -> CatalogItem:
        """Return one prompt, resource or skill the caller may read.

        ``ident`` is a prompt name or a resource URI. The item must be in the
        admitted catalog, visible to the caller and loadable under current
        policy; anything else is the same ``NOT_FOUND`` refusal, so a missing item and a
        hidden one cannot be told apart (ARBORA-FLEET-R040).
        """
        _require_delegate_scope(caller)
        await self._prime((server,))
        item = await self.catalog.get(_surface_item_id(family, server, ident), caller)
        if (
            item is None
            or item.kind not in RELAY_KINDS[family]
            or item.kind == "resource_template"
            or item.server != server
            or not item.required_scopes.issubset(caller.effective_scopes)
            or not await self._loadable(item, caller)
        ):
            from arbora.api.invoke.pipeline import OperationRefused

            raise OperationRefused("NOT_FOUND", {"reason": "fleet item is unavailable"})
        return item

    async def relay_list(
        self, caller: Any, family: str, **params: Any
    ) -> dict[str, Any]:
        """List a connector's prompts or resources through the catalog.

        The result keeps each item's fleet server name and carries the
        per-connector surface report, so a missing surface reads ``not
        served``.
        """
        _require_discover_scope(caller)
        result = await self.find_tools(
            caller, kinds=RELAY_KINDS[family], browse=True, **params
        )
        for row in result["items"]:
            if family == "resources":
                row["uri"] = resource_uri(row["kind"], row["name"])
        return result

    def _session_key(self, caller: Any) -> str:
        key = self._session_key_for(caller)
        if not isinstance(key, str) or not key:
            raise PermissionError("verified MCP session is required")
        return key

    async def search(self, caller: Any, **params: Any) -> dict[str, Any]:
        """Registry service contract for ``fleet.catalog.search``."""
        _require_discover_scope(caller)
        return await self.find_tools(caller, **params)

    async def list(self, caller: Any, **params: Any) -> dict[str, Any]:
        """Registry browse projection over the same filtered catalog."""
        return await self.search(caller, **{**params, "browse": True})

    async def load(
        self,
        caller: Any,
        items: Iterable[str],
        auto_unload: bool = False,
        evict: str | None = None,
    ) -> dict[str, Any]:
        _require_delegate_scope(caller)
        return await self.load_tools(
            caller,
            self._session_key(caller),
            items,
            auto_unload=auto_unload,
            evict=evict,
        )

    async def unload(self, caller: Any, **params: Any) -> dict[str, Any]:
        """Forward to ``unload_tools``; see it for the accepted
        ``items``/``servers``/``kinds``/``all_items`` keywords."""
        _require_delegate_scope(caller)
        return await self.unload_tools(caller, self._session_key(caller), **params)

    async def _coverage_block(self) -> dict[str, Any]:
        """Every registered connector against the endpoints and catalog.

        A missing or unparsable registry fails closed only in strict mode;
        the non-strict block names the unreadable state and no offenders."""
        from arbora.fleet.coverage import FleetCoverageError, check_fleet_coverage

        try:
            registered = tuple(self._coverage_registry())
        except FleetCoverageError:
            return {
                "ok": False,
                "error": "registry_unreadable",
                "registered": [],
                "covered": [],
                "missing": [],
                "zero_tools": [],
            }
        endpoints = tuple(self._coverage_endpoints())
        unadmitted = tuple(self._unadmitted_servers())
        tool_counts = await self.catalog.tool_counts()
        report = check_fleet_coverage(
            registered, endpoints, tool_counts, unadmitted=unadmitted
        )
        return {
            "ok": report.ok,
            "registered": list(report.registered),
            "covered": list(report.covered),
            "missing": [dict(offender) for offender in report.missing],
            "zero_tools": list(report.zero_tools),
        }

    async def _suspended_pins(self, caller: Any, key: str) -> list[dict[str, str]]:
        """Stored pins failing the current re-check, or passing but not loaded.

        A suspended pin stays stored and unloaded; the entry is
        ``{item_id, code}`` (ARBORA-FLEET-R059.5)."""
        store = self._pin_store
        if store is None:
            return []
        try:
            records = await store.list(caller.tenant, caller.principal)
        except Exception:
            return []
        loaded = self.sessions.loaded(key)
        pinned = self.sessions.pinned_items(key)
        suspended: list[dict[str, str]] = []
        for record in records:
            item = await self.catalog.get(record.item_id, caller)
            if item is None or item_binding(item) != record.binding:
                code = "NOT_FOUND"
            else:
                try:
                    loadable = await self._loadable(item, caller)
                except Exception:
                    loadable = False
                    code = "POLICY_UNAVAILABLE"
                else:
                    code = None if loadable else "POLICY_DENIED"
            if code is None and (
                record.item_id not in loaded or record.item_id not in pinned
            ):
                code = "LOAD_CAP_EXCEEDED"
            if code is not None:
                suspended.append({"item_id": record.item_id, "code": code})
        return suspended

    async def status(self, caller: Any, servers: Iterable[str] = ()) -> dict[str, Any]:
        _require_discover_scope(caller)
        key = self._session_key_for(caller)
        snapshot = (
            self.multiplexer_status(key)
            if key
            else {
                "children": dict(self._health()),
                "session": None,
            }
        )
        selected = set(servers)
        if selected:
            children = cast(Mapping[str, Any], snapshot["children"])
            snapshot["children"] = {
                name: state for name, state in children.items() if name in selected
            }
        if key:
            snapshot["suspended"] = await self._suspended_pins(caller, key)
        snapshot["coverage"] = await self._coverage_block()
        return snapshot

    async def find_tools(self, caller: Any, **params: Any) -> dict[str, Any]:
        """Forward to the one typed catalog search; see ``FleetCatalog.search``
        for the accepted ``query``/``kinds``/``servers``/``browse``/
        ``cursor``/``limit``/``context_budget_tokens`` keywords and their
        validation.

        A search filtered to a few named servers first primes their live
        probe, so the items carry ``input_schema``. An unfiltered search
        never starts a fleet sweep. Each returned item the caller has pinned
        carries ``pinned: true`` (ARBORA-FLEET-R059.7)."""
        servers = params.get("servers") or ()
        if isinstance(servers, str):
            servers = (servers,)
        servers = tuple(servers)
        if 0 < len(servers) <= _MAX_PRIMED_SERVERS:
            await self._prime(servers)
        result = await self.catalog.search(caller, **params)
        key = self._session_key_for(caller)
        if key:
            pinned = self.sessions.pinned_items(key)
            if pinned:
                for row in result["items"]:
                    if row.get("id") in pinned:
                        row["pinned"] = True
        return result

    def _forwarder(self, item: CatalogItem) -> Callable[..., Awaitable[Any]]:
        if item.server is None:
            raise ValueError("tool has no server")

        async def call(arguments: Mapping[str, Any], caller: Any) -> Any:
            return await self._invoke(
                "fleet.call",
                {
                    "server": item.server,
                    "tool": item.name,
                    "arguments": dict(arguments),
                },
                caller,
                "mcp",
            )

        return call

    def _loaded_result(self, item: CatalogItem) -> dict[str, Any]:
        result = item.public()
        if item.kind == "tool":
            result.update(
                {
                    "callable_as": self._native_name(item),
                    "input_schema": dict(item.schema),
                    "fallback": {
                        "verb": "act",
                        "op": "fleet.call",
                        "params": {
                            "server": item.server,
                            "tool": item.name,
                            "arguments": {},
                        },
                    },
                }
            )
        elif item.kind == "prompt":
            result["prompt_name"] = self._native_name(item)
            if item.body is not None:
                result["body"] = item.body
        elif item.kind == "resource":
            result["uri"] = self._native_name(item)
        elif item.kind == "skill":
            result["body"] = item.body
        return result

    async def _resolve_loadable_items(
        self, ids: builtins.list[str], caller: Any
    ) -> builtins.list[CatalogItem]:
        resolved: builtins.list[CatalogItem] = []
        for item_id in ids:
            item = await self.catalog.get(item_id, caller)
            if item is None or not await self._loadable(item, caller):
                raise PermissionError(f"item is unavailable for load: {item_id}")
            resolved.append(item)
        return resolved

    def _check_session_cap(
        self, session_key: str, ids: builtins.list[str], evict: str | None
    ) -> None:
        if (
            len(set(ids) | self.sessions.loaded(session_key)) > self.sessions.cap
            and evict != "lru"
        ):
            # The state machine provides the detailed LOAD_CAP_EXCEEDED payload.
            try:
                self.sessions.load(session_key, ids, evict=evict)
            except LoadCapExceeded as exc:
                raise _load_cap_refusal(exc) from exc

    def _register_native_name(self, item: CatalogItem) -> None:
        name = self._native_name(item)
        prior = self._native_ids.get(name)
        if prior is not None and prior != item.id:
            raise ValueError(f"native fleet name collision: {name}")
        self._native_ids[name] = item.id

    async def _mount_item_for_load(self, item: CatalogItem) -> None:
        if item.kind == "connector_item":
            return
        if item.kind in {"tool", "prompt", "resource", "resource_template"}:
            self._register_native_name(item)
        if item.kind == "skill":
            # A skill body is returned in the load result until a native
            # Skills-over-MCP provider is bound for this client profile.
            return
        if item.kind in {"resource", "resource_template"} and self._read_item is None:
            raise RuntimeError("governed fleet read adapter is unavailable")
        await self._mount(item, self._forwarder(item) if item.kind == "tool" else None)

    async def load_tools(
        self,
        caller: Any,
        session_key: str,
        items: Iterable[str],
        *,
        auto_unload: bool = False,
        evict: str | None = None,
    ) -> dict[str, Any]:
        ids = list(dict.fromkeys(items))
        if not ids or len(ids) > 256:
            raise ValueError("load expects 1..256 item ids")
        _require_delegate_scope(caller)
        resolved = await self._resolve_loadable_items(ids, caller)
        self._check_session_cap(session_key, ids, evict)
        for item in resolved:
            await self._mount_item_for_load(item)
        try:
            state = self.sessions.load(
                session_key,
                ids,
                evict=evict,
                auto_unload=auto_unload,
                bindings={item.id: item_binding(item) for item in resolved},
            )
        except LoadCapExceeded as exc:
            raise _load_cap_refusal(exc) from exc
        changed = bool(state["evicted"]) or bool(ids)
        sent = await self._notify(session_key) if changed else True
        self.sessions.notification(session_key, sent)
        return {
            "items": [self._loaded_result(item) for item in resolved],
            **state,
            "notification_sent": sent,
        }

    async def unload_tools(
        self,
        caller: Any,
        session_key: str,
        *,
        items: Iterable[str] = (),
        servers: Iterable[str] = (),
        kinds: Iterable[ItemKind] = (),
        all_items: bool = False,
        include_pinned: bool = False,
    ) -> dict[str, Any]:
        _require_delegate_scope(caller)
        pinned = self.sessions.pinned_items(session_key)
        loaded = self.sessions.retractable(session_key)
        explicit = set(items)
        # An explicit item always unloads, pin included (R059.2).
        targets = explicit & (loaded | pinned)
        server_filter, kind_filter = set(servers), set(kinds)
        skipped_pinned: set[str] = set()
        if all_items or server_filter or kind_filter:
            # The selectors cover the loaded items, and the pinned ones
            # always appear as candidates so the pin can name what it
            # withholds.
            candidates = loaded | pinned
            for item_id in candidates:
                item = await self.catalog.get(item_id, caller)
                if item is None:
                    continue
                if not (
                    all_items
                    or item.server in server_filter
                    or item.kind in kind_filter
                ):
                    continue
                if item_id in pinned and not include_pinned and item_id not in explicit:
                    # The pin withholds the item; name it, never unload it.
                    skipped_pinned.add(item_id)
                    continue
                targets.add(item_id)
        # An explicit unload of a pinned item ends the pin durably, too: the
        # next session must not restore what the caller explicitly dropped.
        # A store failure refuses before the live session changes (R059.2).
        store = self._pin_store
        if store is not None and targets & pinned:
            try:
                for item_id in targets & pinned:
                    await store.delete(caller.tenant, caller.principal, item_id)
            except Exception:
                from arbora.api.invoke.pipeline import OperationRefused

                raise OperationRefused(
                    "UNAVAILABLE", {"reason": "the pin store is unavailable"}
                ) from None
        removed = self.sessions.unload(session_key, targets)
        sent = await self._notify(session_key) if removed else True
        self.sessions.notification(session_key, sent)
        return {
            "unloaded": removed,
            "skipped_pinned": sorted(skipped_pinned),
            "notification_sent": sent,
            "session_total": len(self.sessions.loaded(session_key)),
        }

    async def pin(self, caller: Any, items: Iterable[str]) -> dict[str, Any]:
        """Pin loadable items so they stay loaded across sessions and restarts.

        A pin is never wider than a load by the same caller: every item goes
        through the item scopes and the load policy first. A store failure
        pins nothing and leaves the live session unchanged (R059.6)."""
        from arbora.api.invoke.pipeline import OperationRefused

        ids = list(dict.fromkeys(items))
        if not ids or len(ids) > _MAX_PINNED_ITEMS:
            raise OperationRefused(
                "INVALID_ARGUMENT",
                {"reason": f"pin expects 1..{_MAX_PINNED_ITEMS} item ids"},
            )
        _require_delegate_scope(caller)
        key = self._session_key(caller)
        store = self._pin_store
        if store is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the pin store is unavailable"}
            )
        resolved: builtins.list[CatalogItem] = []
        for item_id in ids:
            item = await self.catalog.get(item_id, caller)
            if item is None:
                # A missing item and a hidden one are the same refusal, so a
                # pin never reveals what exists (R059.6).
                raise OperationRefused(
                    "NOT_FOUND",
                    {"reason": f"{item_id} is not a visible fleet item"},
                )
            try:
                loadable = await self._loadable(item, caller)
            except Exception:
                raise OperationRefused(
                    "POLICY_UNAVAILABLE",
                    {"reason": "the load policy is unavailable"},
                ) from None
            if not loadable:
                raise OperationRefused(
                    "POLICY_DENIED",
                    {"reason": f"{item_id} is not loadable for this caller"},
                )
            resolved.append(item)
        pinned_before = self.sessions.pinned_items(key)
        new_pins = [item_id for item_id in ids if item_id not in pinned_before]
        if len(pinned_before) + len(new_pins) > self.sessions.pinned_cap:
            raise OperationRefused(
                "LOAD_CAP_EXCEEDED",
                {"cap": "pinned", "pinned": sorted(pinned_before | set(ids))},
            )
        for item in resolved:
            await self._mount_item_for_load(item)
        loaded_before = self.sessions.loaded(key)
        newly_loaded = [item_id for item_id in ids if item_id not in loaded_before]
        try:
            self.sessions.load(
                key,
                ids,
                bindings={item.id: item_binding(item) for item in resolved},
            )
            self.sessions.pin(key, ids)
        except LoadCapExceeded as exc:
            raise _load_cap_refusal(exc) from exc
        except ValueError as exc:
            raise OperationRefused("INVALID_ARGUMENT", {"reason": str(exc)}) from exc
        try:
            for item in resolved:
                await store.put(
                    PinRecord(
                        tenant=caller.tenant,
                        principal=caller.principal,
                        item_id=item.id,
                        binding=item_binding(item),
                        pinned_at_ms=pinned_now_ms(),
                    )
                )
        except Exception:
            self.sessions.unpin(key, new_pins)
            self.sessions.unload(key, newly_loaded)
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the pin store is unavailable"}
            ) from None
        changed = bool(new_pins) or bool(newly_loaded)
        sent = await self._notify(key) if changed else True
        self.sessions.notification(key, sent)
        return {
            "pinned": ids,
            "session_total": len(self.sessions.loaded(key)),
            "notification_sent": sent,
        }

    async def unpin(self, caller: Any, items: Iterable[str]) -> dict[str, Any]:
        """Unpin items; an item that is not pinned succeeds under ``not_pinned``."""
        from arbora.api.invoke.pipeline import OperationRefused

        ids = list(dict.fromkeys(items))
        if not ids or len(ids) > _MAX_PINNED_ITEMS:
            raise OperationRefused(
                "INVALID_ARGUMENT",
                {"reason": f"unpin expects 1..{_MAX_PINNED_ITEMS} item ids"},
            )
        _require_delegate_scope(caller)
        key = self._session_key(caller)
        store = self._pin_store
        if store is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the pin store is unavailable"}
            )
        try:
            stored = {
                record.item_id
                for record in await store.list(caller.tenant, caller.principal)
            }
        except Exception:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the pin store is unavailable"}
            ) from None
        pinned_before = self.sessions.pinned_items(key) | stored
        for item_id in ids:
            if item_id not in stored:
                continue
            try:
                await store.delete(caller.tenant, caller.principal, item_id)
            except Exception:
                raise OperationRefused(
                    "UNAVAILABLE", {"reason": "the pin store is unavailable"}
                ) from None
        self.sessions.unpin(key, ids)
        unpinned = sorted(set(ids) & pinned_before)
        not_pinned = sorted(set(ids) - pinned_before)
        return {
            "unpinned": unpinned,
            "not_pinned": not_pinned,
            "session_total": len(self.sessions.loaded(key)),
        }

    async def restore_pins(self, caller: Any, key: str) -> dict[str, Any]:
        """Reload this caller's stored pins before invisible grants are revoked.

        Every record is re-checked with the caller's current scopes and policy;
        a failed check suspends the pin without deleting it, and a pin that
        passes but no longer fits the session is suspended the same way. The
        session is only touched by pins that pass (R059.4/R059.5)."""
        store = self._pin_store
        if store is None:
            return {"restored": [], "suspended": [], "store_unavailable": True}
        try:
            records = await store.list(caller.tenant, caller.principal)
        except Exception:
            return {"restored": [], "suspended": [], "store_unavailable": True}
        restored: builtins.list[str] = []
        suspended: builtins.list[dict[str, str]] = []
        for record in records:
            item = await self.catalog.get(record.item_id, caller)
            if item is None or item_binding(item) != record.binding:
                suspended.append({"item_id": record.item_id, "code": "NOT_FOUND"})
                continue
            try:
                loadable = await self._loadable(item, caller)
            except Exception:
                loadable = False
                code = "POLICY_UNAVAILABLE"
            else:
                code = None if loadable else "POLICY_DENIED"
            if not loadable:
                suspended.append({"item_id": record.item_id, "code": code})
                continue
            try:
                await self._mount_item_for_load(item)
                self.sessions.load(
                    key, [item.id], bindings={item.id: item_binding(item)}
                )
                self.sessions.pin(key, [item.id])
            except LoadCapExceeded:
                suspended.append(
                    {"item_id": record.item_id, "code": "LOAD_CAP_EXCEEDED"}
                )
                continue
            restored.append(item.id)
        if restored:
            self.sessions.notification(key, await self._notify(key))
        return {"restored": restored, "suspended": suspended}

    async def pins_list(
        self,
        caller: Any,
        *,
        principal: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """The admin view: every pin in the caller's tenant, with suspended state.

        The suspended state is read from the owning principal's own session
        key; the admin never impersonates it (R059.8)."""
        from arbora.api.invoke.pipeline import OperationRefused

        if not 1 <= limit <= 100:
            raise ValueError("limit must be in 1..100")
        store = self._pin_store
        if store is None:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the pin store is unavailable"}
            )
        try:
            records, next_cursor = await store.list_tenant(caller.tenant, cursor, limit)
        except Exception:
            raise OperationRefused(
                "UNAVAILABLE", {"reason": "the pin store is unavailable"}
            ) from None
        rows: builtins.list[dict[str, Any]] = []
        for record in records:
            if principal is not None and record.principal != principal:
                continue
            owner_key = f"{caller.tenant}:{record.principal}"
            rows.append(
                {
                    "principal": record.principal,
                    "item_id": record.item_id,
                    "binding": list(record.binding),
                    "pinned_at_ms": record.pinned_at_ms,
                    "suspended": record.item_id not in self.sessions.loaded(owner_key),
                }
            )
        return {"items": rows, "next_cursor": next_cursor}

    async def redeliver_pending(self, session_key: str) -> bool:
        """Resend on the next request for clients that missed list_changed."""
        if not self.sessions.status(session_key)["list_changed_pending"]:
            return True
        sent = await self._notify(session_key)
        self.sessions.redelivered(session_key, sent)
        return sent

    async def revoke_invisible(
        self, caller: Any, session_key: str
    ) -> builtins.list[str]:
        """Drop loaded items whose discovery/load policy changed."""
        removed: builtins.list[str] = []
        for item_id in self.sessions.retractable(session_key):
            item = await self.catalog.get(item_id, caller)
            if (
                item is None
                or self.sessions.binding(session_key, item_id) != item_binding(item)
                or not await self._loadable(item, caller)
            ):
                removed.append(item_id)
        if removed:
            self.sessions.unload(session_key, removed)
            self.sessions.notification(session_key, await self._notify(session_key))
        return sorted(removed)

    async def invalidate_policy_revision(
        self, _revision: str
    ) -> dict[str, builtins.list[str]]:
        """Drop every loaded grant when the PDP revision changes.

        A revision callback has no request caller and cannot safely re-evaluate
        another session's principal. That session may explicitly load again
        under its new policy on its next request.
        """
        removed_by_session: dict[str, builtins.list[str]] = {}
        for key in self.sessions.active_keys():
            removed = self.sessions.unload(key, self.sessions.retractable(key))
            if not removed:
                continue
            removed_by_session[key] = removed
        for key in removed_by_session:
            sent = await self._notify(key)
            self.sessions.notification(key, sent)
        return removed_by_session

    def dispatchable(self, session_key: str, item_id: str) -> bool:
        return self._native_ids.get(item_id) in self.sessions.loaded(session_key)

    def catalog_id_for_native(self, name: str) -> str | None:
        return self._native_ids.get(name)

    async def read_loaded_item(
        self,
        caller: Any,
        session_key: str,
        item_id: str,
        params: Mapping[str, Any],
    ) -> Any:
        """Render one loaded non-tool item under fresh caller policy."""
        _require_delegate_scope(caller)
        if item_id not in self.sessions.loaded(session_key):
            raise PermissionError("item is not loaded in this session")
        item = await self.catalog.get(item_id, caller)
        if item is None or not await self._loadable(item, caller):
            raise PermissionError("item is unavailable")
        if item.kind in {"prompt", "skill"} and item.body is not None:
            return item.body
        if item.kind not in {"prompt", "resource", "resource_template"}:
            raise ValueError("item has no read surface")
        if self._read_item is None:
            raise RuntimeError("governed fleet read adapter is unavailable")
        return await self._read_item(item, params, caller)

    def multiplexer_status(self, session_key: str) -> dict[str, Any]:
        return {
            "children": dict(self._health()),
            "session": self.sessions.status(session_key),
        }
