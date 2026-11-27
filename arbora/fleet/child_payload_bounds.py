"""The bounds a child catalog and a delegated value are admitted within: JSON size bounds, paged listings and bounded descriptors (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import collections.abc as _collections_abc
import hashlib
import json
import logging
import math
import re
import typing as _typing

import fastmcp.exceptions as _fastmcp_exceptions
import fastmcp.tools as _fastmcp_tools

from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    from mcp_types import Tool as MCPTool
import arbora.fleet.child_resilience as _child_resilience
from arbora.security.error_surface import public_error_text

logger = logging.getLogger("mcp_multiplexer")
# The SOLE handle on the MCP protocol types in this module, for two reasons that
# both bite here. (1) MCP SDK v2 re-homed the whole `mcp.types` namespace into the
# standalone `mcp_types` distribution, so `import mcp.types` raises ImportError at
# module scope on an SDK v2 image — and this module is the fleet loader, so that
# takes every child server down with it. `mcp_types_module()` binds whichever the
# installed SDK ships. (2) `mcp` is also a common PARAMETER name in this file (see
# `_register_forwarder`), where it shadows the package outright; a `mcp_types.X`
# attribute chain there resolves against the parameter, not the SDK.
mcp_types = mcp_types_module()


_MAX_DELEGATED_VALUE_BYTES = 4 * 1024 * 1024


_MAX_DELEGATED_NODES = 16_384


_MAX_DELEGATED_DEPTH = 32


#: Resource and prompt listings keep a fixed per-child count bound. A tool
#: catalog has no count bound at all (ARBORA-FLEET-R069): a connector that
#: serves one tool per upstream operation is admitted whole.
_MAX_DISCOVERED_DESCRIPTORS = 2_048


#: One child tool descriptor's structural allowance. Each descriptor is
#: bounded on its own; a large catalog is never truncated (ARBORA-FLEET-R069).
_MAX_CATALOG_NODES = 131_072


# Skills-over-MCP (CONCEPT:MERISTEM-ECO.mcp.skills-over-mcp-provider): a probed
# server's Resources may include ``skill://{name}/SKILL.md`` entries. Bounded
# the same way as the tool catalog so a hostile/misbehaving child cannot force
# an unbounded resource listing into the KG.
_MAX_DISCOVERED_SKILLS = 2_048


_SKILL_RESOURCE_RE = re.compile(r"^skill://(?P<name>[^/]+)/SKILL\.md$")


# A skill body is never read back over the wire: the resident local-skill
# catalog (``arbora.fleet.local_skill_catalog``) is the one source of bodies
# (ARBORA-FLEET-R006.2 to .5).
# Prompts-over-MCP (CONCEPT:MERISTEM-ECO.mcp.cross-process-prompt-harvest — the
# ``prompt://`` sibling of the skill resources above): a probed server's
# Resources may include ``prompt://{provider}/{name}`` entries served by
# ``server_factory._register_prompt_providers``, bounded the same way.
_MAX_DISCOVERED_PROMPTS = 2_048


_PROMPT_RESOURCE_RE = re.compile(r"^prompt://(?P<provider>[^/]+)/(?P<name>[^/]+)$")


# Sentinel for "this decode produced nothing", distinct from a legitimate
# ``None``/``{}`` payload a child may genuinely have returned.
_ABSENT: _typing.Any = object()


# One JSON scalar's flat accounting weight, and the per-collection/per-key
# structural bounds enforced on every node of a delegated value.
_JSON_SCALAR_BYTES = 16


_MAX_JSON_COLLECTION = 4_096


_MAX_JSON_KEY_BYTES = 1_024


def _bounded_json_mapping_bytes(
    current: dict, depth: int, stack: list[tuple[_typing.Any, int]]
) -> int:
    """Validate one mapping node's keys and enqueue its values onto ``stack``."""
    if len(current) > _MAX_JSON_COLLECTION:
        raise _fastmcp_exceptions.ToolError(
            "MCP tool arguments exceed the collection boundary"
        )
    byte_count = 0
    for key, item in current.items():
        if not isinstance(key, str) or len(key.encode("utf-8")) > _MAX_JSON_KEY_BYTES:
            raise _fastmcp_exceptions.ToolError("MCP tool argument keys are invalid")
        byte_count += len(key.encode("utf-8"))
        stack.append((item, depth + 1))
    return byte_count


def _bounded_json_container_bytes(
    current: _typing.Any, depth: int, stack: list[tuple[_typing.Any, int]]
) -> int:
    """Validate one list/dict node and enqueue its children; reject anything else."""
    if isinstance(current, list):
        if len(current) > _MAX_JSON_COLLECTION:
            raise _fastmcp_exceptions.ToolError(
                "MCP tool arguments exceed the collection boundary"
            )
        stack.extend((item, depth + 1) for item in current)
        return 0
    if not isinstance(current, dict):
        raise _fastmcp_exceptions.ToolError(
            "MCP tool arguments must be JSON-compatible"
        )
    return _bounded_json_mapping_bytes(current, depth, stack)


def _bounded_json_node_bytes(
    current: _typing.Any, depth: int, stack: list[tuple[_typing.Any, int]]
) -> int:
    """Account for one JSON node, enqueueing any children onto ``stack``."""
    if current is None or isinstance(current, bool | int):
        return _JSON_SCALAR_BYTES
    if isinstance(current, float):
        if not math.isfinite(current):
            raise _fastmcp_exceptions.ToolError(
                "MCP tool arguments must contain finite numbers"
            )
        return _JSON_SCALAR_BYTES
    if isinstance(current, str):
        return len(current.encode("utf-8"))
    return _bounded_json_container_bytes(current, depth, stack)


def _assert_bounded_json_value(value: _typing.Any, *, max_nodes: int) -> None:
    """Reject oversized or excessively nested JSON-compatible values."""
    stack: list[tuple[_typing.Any, int]] = [(value, 0)]
    nodes = 0
    byte_count = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes or depth > _MAX_DELEGATED_DEPTH:
            raise _fastmcp_exceptions.ToolError(
                "MCP tool arguments exceed the structural boundary"
            )
        byte_count += _bounded_json_node_bytes(current, depth, stack)
        if byte_count > _MAX_DELEGATED_VALUE_BYTES:
            raise _fastmcp_exceptions.ToolError(
                "MCP tool arguments exceed the size boundary"
            )


def assert_bounded_delegated_value(value: _typing.Any) -> None:
    """Reject oversized or excessively nested MCP tool arguments."""

    _assert_bounded_json_value(value, max_nodes=_MAX_DELEGATED_NODES)


def _child_structured_payload(result: _typing.Any) -> _typing.Any:
    """The decoded ``structuredContent`` half of one child result.

    Returns :data:`_ABSENT` when the child sent no structured half at all, so
    the caller can fall back to the text half without confusing that with a
    child that legitimately answered ``None``/``{}``.
    """

    value = getattr(result, "structuredContent", None)
    if value in (None, {}):
        value = getattr(result, "structured_content", None)
    if value in (None, {}):
        return _ABSENT
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    if isinstance(value, str):
        if len(value.encode("utf-8")) > _MAX_DELEGATED_VALUE_BYTES:
            raise _fastmcp_exceptions.ToolError(
                "MCP child result exceeds the size boundary"
            )
        value = json.loads(value)
    return value


def _child_text_payload(result: _typing.Any) -> _typing.Any:
    """The decoded text half of one child result, when it sent no structure."""

    texts = [
        str(getattr(item, "text", ""))
        for item in (getattr(result, "content", None) or [])
        if getattr(item, "text", "")
    ]
    rendered = "\n".join(texts)
    if not rendered or len(rendered.encode("utf-8")) > _MAX_DELEGATED_VALUE_BYTES:
        raise _fastmcp_exceptions.ToolError(
            "MCP child result is unavailable for parent ingestion"
        )
    return json.loads(rendered)


def child_result_payload(result: _typing.Any) -> _typing.Any:
    """Decode one bounded child result without retaining or logging its body."""

    value = _child_structured_payload(result)
    if value is _ABSENT:
        value = _child_text_payload(result)
    assert_bounded_delegated_value(value)
    return value


def _stage_forwarder_components(
    previous_components: dict, forwarders: dict, changed_names: set[str]
) -> dict:
    """The complete provider component registry with changed forwarders swapped in.

    Staged as ONE mapping so a partial native registration can be undone with a
    single provider-registry swap rather than a doomed series of rollback
    ``add_tool`` calls.
    """

    staged = dict(previous_components)
    for key, component in tuple(staged.items()):
        if (
            isinstance(component, _fastmcp_tools.FunctionTool)
            and component.name in changed_names
        ):
            staged.pop(key)
    for forwarder in forwarders.values():
        staged[forwarder.key] = forwarder
    return staged


def _child_error_result(text: str) -> _typing.Any:
    """One ``isError`` CallToolResult carrying a single opaque text label."""

    return mcp_types.CallToolResult.model_validate(
        {
            "content": [mcp_types.TextContent(type="text", text=text)],
            "isError": True,
        }
    )


def _classified_child_error(code: str, message: str) -> _typing.Any:
    """One ``isError`` CallToolResult carrying a typed code and the child's message.

    The gateway reads ``structuredContent.error.code`` as the refusal code and
    redacts and bounds ``message`` (``fleet_detail``) before any caller sees it.
    """

    return mcp_types.CallToolResult.model_validate(
        {
            "content": [mcp_types.TextContent(type="text", text=message)],
            "structuredContent": {"error": {"code": code, "message": message}},
            "isError": True,
        }
    )


def _render_child_failure_leaves(leaves: list[BaseException]) -> str:
    """Render ordered cause labels with credential-shaped details redacted."""
    from arbora.api.errors import fleet_detail

    return "; ".join(
        f"{type(leaf).__name__}: {fleet_detail(str(leaf)) or ''}".rstrip(": ")
        for leaf in leaves
    )


def _child_failure_result(
    server_name: str, original_name: str, exc: BaseException
) -> _typing.Any:
    """Classify a failed one-shot child call by its real cause (ARBORA-FLEET-R015.1).

    The MCP client runs each session inside an anyio task group, so any failure
    (including the child's own JSON-RPC error answer) reaches this point wrapped
    in an ``ExceptionGroup`` whose text is only "unhandled errors in a
    TaskGroup". The leaves are the cause:

    * a child protocol error from a live child that is not a dead-session
      signature means the child ran and refused: ``CHILD_REFUSED`` with the
      child's own message;
    * a transport leaf or a dead-session signature means the child could not
      be reached or dropped the session: ``UNAVAILABLE``;
    * anything else keeps the opaque, correlation-tracked public error.

    The leaf classes and the child's bounded, redacted message are logged so an
    operator sees the cause; credential-shaped text is redacted first.
    """
    from arbora.api.errors import fleet_detail

    leaves = _child_resilience._exc_leaves(exc)
    rendered = _render_child_failure_leaves(leaves)
    logger.warning(
        "fleet child call failed (server=%s tool=%s causes=%s)",
        server_name,
        original_name,
        rendered or type(exc).__name__,
    )
    if any(
        _child_resilience.is_session_dead(leaf)
        or isinstance(leaf, _child_resilience.TRANSPORT_EXCEPTIONS)
        for leaf in leaves
    ):
        return _classified_child_error(
            "UNAVAILABLE", fleet_detail(rendered) or "child transport failed"
        )
    refusals = [leaf for leaf in leaves if isinstance(leaf, _child_resilience.MCPError)]
    if refusals:
        message = fleet_detail(str(refusals[0])) or "child refused the call"
        return _classified_child_error("CHILD_REFUSED", message)
    return _child_error_result(public_error_text(exc))


def _child_required_scopes(
    child_config: _collections_abc.Mapping[str, _typing.Any],
) -> list[str]:
    """One child's declared ``required_scopes``, validated.

    Accepts either a list or a whitespace-separated string; anything else is a
    configuration error rather than an implicitly empty requirement.
    """

    configured_scopes = child_config.get("required_scopes", [])
    if isinstance(configured_scopes, str):
        configured_scopes = configured_scopes.split()
    if not isinstance(configured_scopes, list) or not all(
        isinstance(scope, str) and 1 <= len(scope) <= 128 for scope in configured_scopes
    ):
        raise _fastmcp_exceptions.ToolError("Child MCP scope configuration is invalid")
    return configured_scopes


def _child_tool_admitted(
    tool_name: str, enabled_tools: _typing.Any, disabled_tools: _typing.Any
) -> bool:
    """Whether one child tool passes its catalog entry's enable/disable filters."""

    import fnmatch

    if enabled_tools is not None and not any(
        fnmatch.fnmatch(tool_name, pat) for pat in enabled_tools
    ):
        logger.info("Skipping a non-whitelisted MCP child tool")
        return False
    if disabled_tools and any(
        fnmatch.fnmatch(tool_name, pat) for pat in disabled_tools
    ):
        logger.info("Skipping a disabled MCP child tool")
        return False
    return True


def _bounded_catalog_name(name: _typing.Any) -> bool:
    """Whether one child-supplied catalog identifier is within its boundary."""

    return (
        isinstance(name, str)
        and 1 <= len(name.encode("utf-8")) <= 256
        and all(ord(character) >= 32 for character in name)
    )


def _bounded_tool_annotations(tool: _typing.Any) -> dict | None:
    """One child tool's annotations as a plain dict, or ``None`` when absent."""

    annotations = getattr(tool, "annotations", None)
    if annotations is None or isinstance(annotations, dict):
        return annotations
    model_dump = getattr(annotations, "model_dump", None)
    if not callable(model_dump):
        raise RuntimeError("MCP child tool catalog is invalid")
    dumped = model_dump(mode="json")
    if not isinstance(dumped, dict):
        raise RuntimeError("MCP child tool catalog is invalid")
    return dumped


def _live_tool_entry(original: str, tobj: _typing.Any) -> dict[str, _typing.Any]:
    """One mounted tool's probe row: name, description, schema, meta, annotations.

    The catalog classifies a tool's effect from its annotations; a live-child
    probe without them turns every read-only tool into a write, so a governed
    READ call is refused.
    """
    if tobj is None:
        return {"name": original, "description": "", "inputSchema": {}}
    entry: dict[str, _typing.Any] = {
        "name": original,
        "description": tobj.description or "",
        "inputSchema": tobj.input_schema or {},
    }
    meta = getattr(tobj, "meta", None)
    if meta:
        entry["meta"] = meta
    annotations = _bounded_tool_annotations(tobj)
    if annotations is not None:
        entry["annotations"] = annotations
    return entry


def _bounded_tool_schemas(tool: _typing.Any) -> dict[str, _typing.Any]:
    """Project typed schemas before the catalog's shared size/depth validation."""
    input_schema = getattr(tool, "input_schema", None) or {}
    output_schema = getattr(tool, "output_schema", None)
    if not isinstance(input_schema, dict):
        raise RuntimeError("MCP child tool catalog is invalid")
    schemas = {"inputSchema": input_schema}
    if output_schema is not None:
        if not isinstance(output_schema, dict):
            raise RuntimeError("MCP child tool catalog is invalid")
        schemas["outputSchema"] = output_schema
    return schemas


def _bounded_tool_entry(tool: _typing.Any) -> dict[str, _typing.Any]:
    """Project and validate ONE child tool descriptor."""

    name = getattr(tool, "name", None)
    description = getattr(tool, "description", "") or ""
    annotations = _bounded_tool_annotations(tool)
    if not _bounded_catalog_name(name) or not isinstance(description, str):
        raise RuntimeError("MCP child tool catalog is invalid")
    item: dict[str, _typing.Any] = {
        "name": name,
        "description": description,
        **_bounded_tool_schemas(tool),
    }
    # The same ``meta`` pass-through the live entry keeps: the catalog reads
    # the connector group from ``scion/upstream`` (ARBORA-FLEET-R061).
    meta = getattr(tool, "meta", None)
    if meta:
        item["meta"] = meta
    if annotations is not None:
        item["annotations"] = annotations
    return item


class FleetCatalogPageError(RuntimeError):
    """One page of a child's ``tools/list`` could not be read.

    Typed (ARBORA-FLEET-R068): the listing is refused whole, never served
    partial, so a connector can never silently lose tools. The probe records
    it as the server's error and the primer retries; the last good catalog
    keeps serving meanwhile.
    """


def _bounded_tool_catalog(raw_tools: _typing.Any) -> list[dict[str, _typing.Any]]:
    """Project one child catalog into a bounded, JSON-compatible shape.

    The MCP SDK decodes remote responses before returning them to us.  This
    boundary prevents a child from turning that decoded value into an
    unbounded in-memory/KG catalog: names, descriptions, schemas, and
    annotations are validated per descriptor before any caller can cache or
    register them. The tool count has no bound (ARBORA-FLEET-R069).
    """

    if not isinstance(raw_tools, list | tuple):
        raise RuntimeError("MCP child tool catalog exceeded its boundary")
    tools: list[dict[str, _typing.Any]] = [
        _bounded_tool_entry(tool) for tool in raw_tools
    ]
    try:
        for entry in tools:
            _assert_bounded_json_value(entry, max_nodes=_MAX_CATALOG_NODES)
    except _fastmcp_exceptions.ToolError:
        raise RuntimeError("MCP child tool catalog exceeded its boundary") from None
    return tools


def _next_cursor(result: _typing.Any) -> str | None:
    """A ``tools/list`` page's continuation cursor, or ``None`` on the last."""
    cursor = getattr(result, "next_cursor", None)
    return cursor if isinstance(cursor, str) and cursor else None


async def _list_page(
    session: _typing.Any, method: str, cursor: str | None
) -> _typing.Any:
    """One page of a child ``*/list``; a continuation page failure is typed.

    The first page raises unchanged, so an optional family a child does not
    implement still reads as "method not found".
    """
    call = getattr(session, method)
    if cursor is None:
        return await call()
    try:
        return await call(params=mcp_types.PaginatedRequestParams(cursor=cursor))
    except Exception as exc:
        raise FleetCatalogPageError(
            f"MCP child {method} page failed ({type(exc).__name__})"
        ) from exc


async def _list_all(session: _typing.Any, method: str, field: str) -> list[_typing.Any]:
    """Every entry of one child list family, followed to its last page.

    ARBORA-FLEET-R068/R068.1: tools, resources, resource templates and prompts
    are read cursor by cursor until a page has no ``nextCursor``. A failed
    continuation page or a repeated cursor refuses the listing whole with
    :class:`FleetCatalogPageError`; a partial list is never returned.
    """
    result = await _list_page(session, method, None)
    entries = list(getattr(result, field))
    seen: set[str] = set()
    cursor = _next_cursor(result)
    while cursor is not None:
        if cursor in seen:
            raise FleetCatalogPageError(f"MCP child {method} cursor repeated")
        seen.add(cursor)
        result = await _list_page(session, method, cursor)
        entries.extend(getattr(result, field))
        cursor = _next_cursor(result)
    return entries


async def _list_all_tools(session: _typing.Any) -> list[_typing.Any]:
    """Every tool a child serves (ARBORA-FLEET-R068)."""
    return await _list_all(session, "list_tools", "tools")


def _tool_catalog_digest(tools: list[MCPTool]) -> str:
    """Return a stable digest of the client-visible portion of one child catalog.

    A child generation already pays for ``tools/list`` as part of its bounded
    connection handshake.  Comparing that result here lets the multiplexer
    refresh only changed forwarding schemas, without polling a provider from
    ordinary tool calls or churning probe/embedding caches after an equivalent
    reconnect.
    """
    catalog = _bounded_tool_catalog(tools)
    for entry, tool in zip(catalog, tools, strict=True):
        meta = getattr(tool, "meta", None)
        if meta is not None:
            _assert_bounded_json_value(meta, max_nodes=_MAX_CATALOG_NODES)
            entry["meta"] = meta
    # A provider is allowed to return its catalog in a different order after a
    # reconnect.  Order is not part of a forwarding schema, so make the
    # no-op comparison insensitive to it and avoid needless cache/host-tool
    # churn on an otherwise equivalent generation.
    catalog.sort(key=lambda entry: entry["name"])
    canonical = json.dumps(
        catalog, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _assert_bounded_resource_list(raw_resources: _typing.Any) -> None:
    """Reject a child ``resources/list`` payload that is not a bounded sequence."""

    if not isinstance(raw_resources, list | tuple):
        raise RuntimeError("MCP child resource catalog is invalid")
    if len(raw_resources) > _MAX_DISCOVERED_DESCRIPTORS:
        raise RuntimeError("MCP child resource catalog exceeded its boundary")


def _bounded_descriptor_catalog(
    values: _typing.Any, *, key: str, family: str
) -> list[dict[str, _typing.Any]]:
    """Project one native MCP descriptor family through the shared boundary."""
    if (
        not isinstance(values, list | tuple)
        or len(values) > _MAX_DISCOVERED_DESCRIPTORS
    ):
        raise RuntimeError(f"MCP child {family} catalog exceeded its boundary")
    projected: list[dict[str, _typing.Any]] = []
    for value in values:
        entry = _descriptor_mapping(value, key)
        if not _bounded_catalog_name(entry.get(key)):
            raise RuntimeError(f"MCP child {family} catalog is invalid")
        projected.append(entry)
    try:
        _assert_bounded_json_value(projected, max_nodes=_MAX_CATALOG_NODES)
    except _fastmcp_exceptions.ToolError:
        raise RuntimeError(
            f"MCP child {family} catalog exceeded its boundary"
        ) from None
    return projected


def _descriptor_mapping(value: _typing.Any, key: str) -> dict[str, _typing.Any]:
    """Convert one decoded descriptor without trusting a concrete SDK class."""
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        mapped = dump(mode="json", by_alias=True, exclude_none=True)
        if isinstance(mapped, dict):
            return mapped
    attribute = "uri_template" if key == "uriTemplate" else key
    identity = getattr(value, attribute, None)
    entry = {key: str(identity) if identity is not None else ""}
    for field in ("name", "description"):
        field_value = getattr(value, field, None)
        if isinstance(field_value, str) and field_value:
            entry[field] = field_value
    return entry


def _bounded_skill_entry(resource: _typing.Any) -> dict[str, _typing.Any] | None:
    """Project ONE ``skill://`` resource, or ``None`` when it is not a skill."""

    uri = getattr(resource, "uri", None)
    uri_text = str(uri) if uri is not None else ""
    match = _SKILL_RESOURCE_RE.match(uri_text)
    if not match:
        return None
    name = match.group("name")
    description = getattr(resource, "description", "") or ""
    if not _bounded_catalog_name(name) or not isinstance(description, str):
        raise RuntimeError("MCP child resource catalog is invalid")
    return {"name": name, "uri": uri_text, "description": description}


def _bounded_prompt_entry(resource: _typing.Any) -> dict[str, _typing.Any] | None:
    """Project ONE ``prompt://`` resource, or ``None`` when it is not a prompt."""

    uri = getattr(resource, "uri", None)
    uri_text = str(uri) if uri is not None else ""
    match = _PROMPT_RESOURCE_RE.match(uri_text)
    if not match:
        return None
    provider = match.group("provider")
    name = match.group("name")
    description = getattr(resource, "description", "") or ""
    if (
        not _bounded_catalog_name(name)
        or not _bounded_catalog_name(provider)
        or not isinstance(description, str)
    ):
        raise RuntimeError("MCP child resource catalog is invalid")
    return {
        "name": name,
        "provider": provider,
        "uri": uri_text,
        "description": description,
    }


def _bounded_skill_catalog(raw_resources: _typing.Any) -> list[dict[str, _typing.Any]]:
    """Project a probed child's Resources into its Skills-over-MCP subset.

    A fastmcp-4 ``SkillProvider``/``ClaudeSkillsProvider`` exposes each skill
    as ``skill://{name}/SKILL.md`` (+ a sibling ``_manifest`` resource this
    projection ignores — the manifest is fetched on demand, not during probe).
    Non-skill resources are silently dropped: this function answers "which
    skills does this server serve", not "list every resource". Bounded and
    validated exactly like :func:`_bounded_tool_catalog` so a hostile/
    misbehaving child cannot force an unbounded catalog into the KG.
    """
    _assert_bounded_resource_list(raw_resources)

    skills: list[dict[str, _typing.Any]] = []
    for resource in raw_resources:
        entry = _bounded_skill_entry(resource)
        if entry is None:
            continue
        skills.append(entry)
        if len(skills) > _MAX_DISCOVERED_SKILLS:
            raise RuntimeError("MCP child skill catalog exceeded its boundary")
    try:
        _assert_bounded_json_value(skills, max_nodes=_MAX_CATALOG_NODES)
    except _fastmcp_exceptions.ToolError:
        raise RuntimeError("MCP child skill catalog exceeded its boundary") from None
    return skills


def _bounded_prompt_catalog(raw_resources: _typing.Any) -> list[dict[str, _typing.Any]]:
    """Project a probed child's Resources into its Prompts-over-MCP subset.

    CONCEPT:MERISTEM-ECO.mcp.cross-process-prompt-harvest — the ``prompt://``
    sibling of :func:`_bounded_skill_catalog`.
    ``server_factory._register_prompt_providers`` exposes each of a server's
    own ``prompts/*.json`` files as ``prompt://{provider}/{name}``.
    Non-prompt resources (including ``skill://`` ones) are silently dropped:
    this function answers "which prompts does this server serve", not "list
    every resource". Bounded and validated exactly like
    :func:`_bounded_skill_catalog` so a hostile/misbehaving child cannot
    force an unbounded catalog into the KG.
    """
    _assert_bounded_resource_list(raw_resources)

    prompts: list[dict[str, _typing.Any]] = []
    for resource in raw_resources:
        entry = _bounded_prompt_entry(resource)
        if entry is None:
            continue
        prompts.append(entry)
        if len(prompts) > _MAX_DISCOVERED_PROMPTS:
            raise RuntimeError("MCP child prompt catalog exceeded its boundary")
    try:
        _assert_bounded_json_value(prompts, max_nodes=_MAX_CATALOG_NODES)
    except _fastmcp_exceptions.ToolError:
        raise RuntimeError("MCP child prompt catalog exceeded its boundary") from None
    return prompts


def _resource_body_text(result: _typing.Any) -> str:
    """Extract the text payload of one ``resources/read`` result.

    CONCEPT:MERISTEM-ECO.mcp.cross-process-skill-harvest — a fastmcp-4
    ``SkillProvider`` serves ``skill://{name}/SKILL.md`` as ``text/markdown``,
    so the body arrives as a ``TextResourceContents``. A binary/blob payload is
    NOT a skill instruction body and is rejected rather than coerced, so a
    child cannot smuggle an unusable resource into the runnable set.
    """
    contents = getattr(result, "contents", None)
    if not isinstance(contents, list | tuple) or not contents:
        raise RuntimeError("skill resource returned no contents")
    parts: list[str] = []
    for item in contents:
        text = getattr(item, "text", None)
        if text is None:
            raise RuntimeError("skill resource body is not text")
        if not isinstance(text, str):
            raise RuntimeError("skill resource body is not text")
        parts.append(text)
    return "\n".join(parts)
