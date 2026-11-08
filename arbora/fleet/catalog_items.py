"""One caller-filtered catalog for the dynamic fleet surface."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from arbora.fleet.surfaces import count_items, surface_report

ItemKind = Literal[
    "tool", "prompt", "resource", "resource_template", "skill", "connector_item"
]
CredentialMode = Literal["delegated", "service"]
Visible = Callable[["CatalogItem", Any], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class CatalogItem:
    """A stable descriptor; body/credentials never enter discovery results."""

    id: str
    kind: ItemKind
    name: str
    description: str = ""
    server: str | None = None
    schema: Mapping[str, Any] = field(default_factory=dict)
    schema_fingerprint: str | None = None
    required_scopes: frozenset[str] = frozenset()
    body: str | None = field(default=None, repr=False)
    op: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    annotations: Mapping[str, Any] | None = field(default=None, repr=False)
    effect_override: str | None = field(default=None, repr=False)
    credential_mode: CredentialMode = field(default="delegated", repr=False)
    executor_scopes: frozenset[str] = field(default=frozenset(), repr=False)
    subject_id: str | None = field(default=None, repr=False)
    #: The upstream application a proxying connector marks the tool with
    #: (``scion/upstream`` namespace); one connector group to load by.
    group: str | None = field(default=None, repr=False)
    #: A prompt's ``title``, declared ``arguments`` and ``_meta`` (its role,
    #: goal, topic, tone, style, version and agent definition), as the
    #: connector lists them (ARBORA-FLEET-R076).
    details: Mapping[str, Any] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
        }
        if self.server is not None:
            result["server"] = self.server
        if self.group is not None:
            result["group"] = self.group
        if self.schema:
            result["input_schema"] = dict(self.schema)
        if self.schema_fingerprint is not None:
            result["schema_fingerprint"] = self.schema_fingerprint
        if self.op is not None:
            result["op"] = self.op
            result["params"] = dict(self.params)
        if self.details:
            result.update(self.details)
        return result


def _rank(item: CatalogItem, query: str) -> int:
    terms = query.lower().split()
    text = f"{item.name} {item.description} {item.server or ''}".lower()
    return sum(3 if term in item.name.lower() else 1 for term in terms if term in text)


def _validate_search_request(
    query: str, browse: bool, limit: int, context_budget_tokens: int | None
) -> None:
    if not query and not browse:
        raise ValueError("query or browse=true is required")
    if not 1 <= limit <= 100:
        raise ValueError("limit must be in 1..100")
    if context_budget_tokens is not None and context_budget_tokens < 1:
        raise ValueError("context budget must be positive")


def _passes_filters(
    item: CatalogItem,
    kind_filter: set[ItemKind],
    server_filter: set[str],
    group_filter: set[str],
    query: str,
) -> bool:
    if kind_filter and item.kind not in kind_filter:
        return False
    if server_filter and item.server not in server_filter:
        return False
    if group_filter and item.group not in group_filter:
        return False
    return not (query and _rank(item, query) == 0)


def _paginate(
    items: list[CatalogItem], cursor: str | None, limit: int
) -> tuple[list[CatalogItem], str | None]:
    start = 0
    if cursor is not None:
        ids = [item.id for item in items]
        if cursor not in ids:
            raise ValueError("catalog cursor expired")
        start = ids.index(cursor) + 1
    page = items[start : start + limit]
    next_cursor = page[-1].id if start + limit < len(items) and page else None
    return page, next_cursor


class FleetCatalog:
    """Merge EG, child and SDK descriptors through one visibility decision."""

    def __init__(
        self,
        sources: Iterable[Callable[[], Awaitable[Iterable[CatalogItem]]]],
        visible: Visible,
    ):
        self._sources = tuple(sources)
        self._visible = visible

    async def _authorized(self, item: CatalogItem, caller: Any) -> bool:
        scopes = caller.effective_scopes
        return (
            "mcp:discover" in scopes
            and item.required_scopes.issubset(scopes)
            and await self._visible(item, caller)
        )

    async def get(self, item_id: str, caller: Any) -> CatalogItem | None:
        item = await self._raw_get(item_id)
        if item is not None and await self._authorized(item, caller):
            return item
        return None

    async def _raw_get(self, item_id: str) -> CatalogItem | None:
        """Return source metadata only; the caller must apply its own action policy."""
        for source in self._sources:
            for item in await source():
                if item.id == item_id:
                    return item
        return None

    async def search(
        self,
        caller: Any,
        *,
        query: str = "",
        kinds: Iterable[ItemKind] = (),
        servers: Iterable[str] = (),
        groups: Iterable[str] = (),
        browse: bool = False,
        cursor: str | None = None,
        limit: int = 20,
        context_budget_tokens: int | None = None,
    ) -> dict[str, Any]:
        servers = tuple(servers)
        groups = tuple(groups)
        # An empty query scoped to named servers or groups lists those items:
        # it is a browse, not a malformed search.
        browse = browse or (not query and bool(servers or groups))
        _validate_search_request(query, browse, limit, context_budget_tokens)
        matched = await self._matching_items(caller, query, kinds, servers, groups)
        items = sorted(
            matched.values(), key=lambda item: (-_rank(item, query), item.id)
        )
        if context_budget_tokens is not None:
            limit = min(limit, max(1, context_budget_tokens // 80))
        page, next_cursor = _paginate(items, cursor, limit)
        return {
            "items": [item.public() for item in page],
            "next_cursor": next_cursor,
            "servers": await self._server_surfaces(caller, matched, servers, page),
        }

    async def group_items(
        self, caller: Any, groups: Iterable[str]
    ) -> tuple[CatalogItem, ...]:
        """The caller-visible tool items of the named groups; no paging.

        An unknown group yields nothing, never an error (ARBORA-FLEET-R061)."""
        group_filter = set(groups)
        matched: dict[str, CatalogItem] = {}
        for source in self._sources:
            for item in await source():
                if (
                    item.kind != "tool"
                    or not item.group
                    or item.group not in group_filter
                    or not await self._authorized(item, caller)
                ):
                    continue
                previous = matched.get(item.id)
                if previous is not None and previous != item:
                    raise ValueError(f"conflicting fleet descriptor: {item.id}")
                matched[item.id] = item
        return tuple(sorted(matched.values(), key=lambda item: item.id))

    async def tool_counts(self) -> dict[str, int]:
        """Admitted ``tool`` items per catalog server.

        Every server holding at least one admitted item appears, with zero for
        a server whose catalog carries no tool; a server with no admitted
        entry is absent, which is how the coverage gate tells ``not_admitted``
        from ``zero_tools`` (ARBORA-FLEET-R060.2). Reads the sources only: no
        priming, no child started, no caller."""
        counts: dict[str, int] = {}
        for source in self._sources:
            for item in await source():
                if item.server is None:
                    continue
                if item.server not in counts:
                    counts[item.server] = 0
                if item.kind == "tool":
                    counts[item.server] += 1
        return counts

    async def server_surfaces(
        self, caller: Any, servers: Iterable[str]
    ) -> dict[str, Any]:
        """Surface counts of each named server, over every caller-visible item.

        A named server with no visible item reads every surface ``not served``
        (ARBORA-FLEET-R040.3)."""
        return await self._server_surfaces(caller, {}, servers, ())

    async def _server_surfaces(
        self,
        caller: Any,
        matched: Mapping[str, CatalogItem],
        servers: Iterable[str],
        page: Iterable[CatalogItem],
    ) -> dict[str, Any]:
        """Per-connector surface counts for the named or listed servers.

        Counts cover every caller-visible item of a server, not only this
        page or kind. A surface with no items reads ``not served``, never a
        silent zero (ARBORA-FLEET-R040).
        """
        wanted = set(servers) | {item.server for item in page if item.server}
        visible: dict[str, CatalogItem] = {
            item_id: item for item_id, item in matched.items() if item.server in wanted
        }
        if wanted:
            for source in self._sources:
                for item in await source():
                    if (
                        item.server in wanted
                        and item.id not in visible
                        and await self._authorized(item, caller)
                    ):
                        visible[item.id] = item
        by_server: dict[str, list[tuple[str, str]]] = {name: [] for name in wanted}
        for item in visible.values():
            by_server[str(item.server)].append((item.kind, item.name))
        return {
            name: surface_report(count_items(pairs))
            for name, pairs in sorted(by_server.items())
        }

    async def _matching_items(
        self,
        caller: Any,
        query: str,
        kinds: Iterable[ItemKind],
        servers: Iterable[str],
        groups: Iterable[str] = (),
    ) -> dict[str, CatalogItem]:
        kind_filter, server_filter, group_filter = set(kinds), set(servers), set(groups)
        matched: dict[str, CatalogItem] = {}
        for source in self._sources:
            for item in await source():
                if not _passes_filters(
                    item, kind_filter, server_filter, group_filter, query
                ):
                    continue
                if await self._authorized(item, caller):
                    previous = matched.get(item.id)
                    if previous is not None and previous != item:
                        raise ValueError(f"conflicting fleet descriptor: {item.id}")
                    matched[item.id] = item
        return matched


def items_from_eg_catalog(snapshot: Any) -> tuple[CatalogItem, ...]:
    """Project verified AgentComponent records without trusting probe metadata as authority."""
    kinds: dict[str, ItemKind] = {
        "tool": "tool",
        "skill": "skill",
        "mcp_prompt": "prompt",
        "mcp_resource": "resource",
    }
    items: list[CatalogItem] = []
    for server in snapshot.servers:
        for provided in server.provides:
            entry = provided.entry
            kind = kinds.get(entry.kind)
            if kind is None:
                continue
            body = (
                provided.content.body.decode("utf-8")
                if kind in {"skill", "prompt"}
                else None
            )
            item = CatalogItem(
                id=f"fleet:{kind}:{entry.server_name}/{entry.upstream_name}",
                kind=kind,
                name=entry.upstream_name,
                description=entry.summary,
                server=entry.server_name,
                body=body,
            )
            items.append(
                _with_engine_tool_contract(item, provided) if kind == "tool" else item
            )
    return tuple(items)


def _engine_input_schema(body: bytes) -> dict[str, Any]:
    """The input schema an engine tool body records, or ``{}``."""
    try:
        document = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    schema = document.get("input_schema") if isinstance(document, dict) else None
    return schema if isinstance(schema, dict) else {}


def _with_engine_tool_contract(item: CatalogItem, provided: Any) -> CatalogItem:
    """Fill a tool item's schema and hints from the engine (ARBORA-FLEET-R072).

    The engine holds the whole imported pack: each tool's content body carries
    its input schema and its entry carries the declared behaviour hints. The
    served catalog therefore needs no process-resident copy of a connector's
    tool list to know a tool's parameters or that it is read-only. A live
    probe of the same tool still wins (``_merge_probed_item``).
    """
    schema = _engine_input_schema(provided.content.body)
    hints = getattr(provided.entry, "tool_hints", None)
    return replace(
        item,
        schema=schema,
        schema_fingerprint=compute_schema_fingerprint(schema) if schema else None,
        annotations=dict(hints) if hints else None,
    )


def compute_schema_fingerprint(schema: Mapping[str, Any]) -> str:
    """Canonical SHA-256 digest of a tool's actual served input schema.

    ARBORA-FLEET-R002: every fleet tool pin's schema fingerprint must be
    computed from the tool's real served schema rather than an empty
    placeholder. An empty mapping never had a schema served for it, so
    fingerprinting one would certify a placeholder as if it were real;
    refuse instead.
    """
    if not schema:
        raise ValueError("cannot fingerprint an empty schema")
    canonical = json.dumps(dict(schema), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


_PROBE_IDENTITY_KEYS: dict[str, tuple[str, ...]] = {
    "resource": ("uri", "name"),
    "resource_template": ("uriTemplate", "name"),
}
_SKILL_SCHEME = "skill://"
_SKILL_MAIN = "/SKILL.md"


def _is_skill_resource(row: Mapping[str, Any]) -> bool:
    uri = str(row.get("uri") or "")
    return uri.startswith(_SKILL_SCHEME) and uri.endswith(_SKILL_MAIN)


#: The probe-row key a proxying connector marks each upstream tool with
#: (SCION-CONNECTOR-CONTROL-R914.5).
_UPSTREAM_META_KEY = "scion/upstream"
#: A connector group name: 2 to 24 lowercase letters and digits.
_GROUP = re.compile(r"[a-z0-9]{2,24}")


def _upstream_group(row: Mapping[str, Any]) -> str | None:
    """The connector group a probe row marks, or ``None`` when unmarked.

    The namespace arrives under the tool ``meta`` key ``scion/upstream``.
    Anything that is not a strict namespace string leaves the item ungrouped."""
    meta = row.get("meta")
    if not isinstance(meta, Mapping):
        return None
    upstream = meta.get(_UPSTREAM_META_KEY)
    if not isinstance(upstream, Mapping):
        return None
    namespace = upstream.get("namespace")
    if isinstance(namespace, str) and _GROUP.fullmatch(namespace):
        return namespace
    return None


#: The ``prompts/list`` fields a prompt item carries whole (ARBORA-FLEET-R076).
PROMPT_DETAIL_FIELDS = ("title", "arguments", "_meta")


def prompt_details(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """A listed prompt's title, arguments and ``_meta``; ``None`` when absent."""
    details = {key: row[key] for key in PROMPT_DETAIL_FIELDS if row.get(key)}
    return details or None


def prompt_argument_schema(arguments: Any) -> dict[str, Any]:
    """The object schema of a prompt's declared string arguments."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for argument in arguments or ():
        name = argument.get("name") if isinstance(argument, Mapping) else None
        if not isinstance(name, str) or not name:
            continue
        description = argument.get("description")
        properties[name] = {"type": "string"} | (
            {"description": description} if isinstance(description, str) else {}
        )
        if argument.get("required") is True:
            required.append(name)
    if not properties:
        return {}
    return {"type": "object", "properties": properties} | (
        {"required": required} if required else {}
    )


def _probed_item(
    server: str, kind: ItemKind, row: Mapping[str, Any]
) -> CatalogItem | None:
    # A resource is read by its URI, so its catalog identity is the URI, not
    # the display name (ARBORA-FLEET-R040): ``resource_uri`` reads it back.
    keys = _PROBE_IDENTITY_KEYS.get(kind, ("name",))
    name = next((row[key] for key in keys if row.get(key)), None)
    if not isinstance(name, str) or not name:
        return None
    schema = row.get("inputSchema") or {}
    details = prompt_details(row) if kind == "prompt" else None
    if details is not None:
        schema = prompt_argument_schema(details.get("arguments"))
    return CatalogItem(
        id=f"fleet:{kind}:{server}/{name}",
        kind=kind,
        name=name,
        description=str(row.get("description") or ""),
        server=server,
        schema=schema,
        schema_fingerprint=(
            compute_schema_fingerprint(schema) if kind == "tool" and schema else None
        ),
        annotations=(
            dict(row["annotations"])
            if isinstance(row.get("annotations"), Mapping)
            else None
        ),
        group=_upstream_group(row),
        details=details,
    )


def items_from_child_probe(
    server: str, info: Mapping[str, Any]
) -> tuple[CatalogItem, ...]:
    """Project live child protocol families; callers still need EG admission."""
    families: tuple[tuple[str, ItemKind], ...] = (
        ("tools", "tool"),
        ("native_prompts", "prompt"),
        ("resources", "resource"),
        ("resource_templates", "resource_template"),
        ("skills", "skill"),
    )
    if info.get("error"):
        return ()
    # ``skill://<name>/SKILL.md`` resources are the skills family, projected
    # once as ``skill`` items by name; they are not also plain resources.
    items = (
        _probed_item(server, kind, row)
        for field_name, kind in families
        for row in info.get(field_name, ())
        if not (kind == "resource" and _is_skill_resource(row))
    )
    return tuple(item for item in items if item is not None)


PACK_ANNOTATION_FIELDS: tuple[str, ...] = (
    "capability",
    "schema_digest",
    "modality",
    "cost",
    "latency",
)


def _pack_annotations(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Required catalog annotations for a fleet pack item (ARBORA-FLEET-R004).

    Every connector pack item's catalog entry must carry its capability,
    schema digest, modality, cost and latency so pack items are comparable
    fleet-wide. An entry missing, or emptying, one of these fails closed
    rather than publishing an incomplete catalog row; ``annotations`` may
    carry them as a nested mapping, or each may sit directly on the entry.
    """
    nested = entry.get("annotations")
    source: Mapping[str, Any] = nested if isinstance(nested, Mapping) else entry
    annotations: dict[str, Any] = {}
    for field_name in PACK_ANNOTATION_FIELDS:
        value = source.get(field_name)
        if value is None or (isinstance(value, str) and not value):
            raise ValueError(
                f"connector pack item missing required annotation: {field_name}"
            )
        annotations[field_name] = value
    return annotations


def connector_items(entries: Iterable[Mapping[str, Any]]) -> tuple[CatalogItem, ...]:
    """Expose SDK pack entries as typed operation pointers, never native tools."""
    items: list[CatalogItem] = []
    for entry in entries:
        pack, name, op = entry["pack"], entry["name"], entry["op"]
        items.append(
            CatalogItem(
                id=f"connector:{pack}/{name}",
                kind="connector_item",
                name=name,
                description=entry.get("description", ""),
                op=op,
                params=entry.get("params", {}),
                required_scopes=frozenset(entry.get("required_scopes", ())),
                annotations=_pack_annotations(entry),
            )
        )
    return tuple(items)
