"""Server defaults and the request, header and environment filters that narrow the served components (ARBORA-HOST-R038.10)."""

from __future__ import annotations

import logging
import re
from typing import Any

from arbora.api.errors import not_served
from arbora.core.environment import read_env

logger = logging.getLogger(__name__)


DEFAULT_HOST = str(read_env("HOST"))


DEFAULT_PORT = int(read_env("PORT"))


DEFAULT_TRANSPORT = read_env("TRANSPORT")


_NETWORK_TRANSPORTS = frozenset({"streamable-http", "sse"})


_ALL_TRANSPORTS = ("stdio", "streamable-http", "sse")


_FILTER_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")


_NO_HTTP_REQUEST = "No active HTTP request found."


class UnsupportedAuthTypeError(ValueError):
    """``--auth-type``/``AUTH_TYPE`` named a mode this factory cannot configure.

    Fail closed (BUG-CX-023). ``_configure_auth`` used to fall off the end of
    its dispatch chain and return ``None`` -- which FastMCP reads as "no auth"
    -- so an unrecognised value silently produced an UNAUTHENTICATED server.
    ``--auth-type`` carries ``choices=``, but argparse does **not** validate a
    *default*, and the default is ``read_env("AUTH_TYPE")``: a typo'd
    ``AUTH_TYPE`` environment variable therefore reached the dispatch chain
    completely unchecked. Refusing is the only safe reading of an auth mode we
    do not understand.

    Subclasses :class:`ValueError` to match this module's existing idiom for an
    invalid configuration value (and ``runtime.UnsupportedToolFieldError``).
    """


def _bounded_filter_values(values: list[str]) -> list[str]:
    """Parse bounded client visibility filters; these may only narrow access."""
    parsed: list[str] = []
    for raw in values:
        if len(str(raw).encode("utf-8")) > 16_384:
            raise ValueError("MCP visibility filter is too large")
        for part in str(raw).split(","):
            value = part.strip()
            if not value:
                continue
            if value.lower() == "all" or not _FILTER_VALUE_RE.fullmatch(value):
                raise ValueError("MCP visibility filter is invalid")
            parsed.append(value)
            if len(parsed) > 256:
                raise ValueError("MCP visibility filter has too many values")
    return list(dict.fromkeys(parsed))


def _narrow_values(current: list[str], requested: list[str]) -> list[str]:
    """Intersect an existing allowlist, or establish one when none exists."""
    if not requested:
        return current
    if not current:
        return requested
    requested_set = set(requested)
    narrowed = [value for value in current if value in requested_set]
    return narrowed or ["__empty_filter_intersection__"]


def _union_values(current: list[str], additions: list[str]) -> list[str]:
    """Union deny lists without allowing a caller to remove server policy."""
    return list(dict.fromkeys([*current, *additions]))


def _split_csv(raw: str) -> list[str]:
    """Split a comma-separated env/CLI/query value into trimmed, non-empty parts."""
    return [x.strip() for x in raw.split(",") if x.strip()]


def _csv_setting(key: str) -> list[str]:
    """Read a comma-separated env-var default via `read_env()`, split and trimmed."""
    raw = read_env(key)
    return _split_csv(raw) if raw else []


def _env_filter_defaults() -> tuple[list[str], list[str], list[str], list[str]]:
    """Starting (enabled_tools, disabled_tools, enabled_tags, disabled_tags)
    lists from the MCP_*_TOOLS / MCP_*_TAGS environment-variable defaults."""
    return (
        _csv_setting("MCP_ENABLED_TOOLS"),
        _csv_setting("MCP_DISABLED_TOOLS"),
        _csv_setting("MCP_ENABLED_TAGS"),
        _csv_setting("MCP_DISABLED_TAGS"),
    )


def _single_or_empty(value: str | None) -> list[str]:
    return [value] if value else []


def _collect_multi_values(source: Any, keys: list[str]) -> list[str]:
    """Collect comma-separated values across query-param/header ``keys``,
    using whichever of ``getlist``/``get`` the source (Starlette QueryParams
    or Headers) implements."""
    collected: list[str] = []
    for key in keys:
        vals = (
            source.getlist(key)
            if hasattr(source, "getlist")
            else _single_or_empty(source.get(key))
        )
        for val in vals:
            if val:
                collected.extend(_split_csv(val))
    return collected


def _first_present(source: Any, keys: list[str]) -> str | None:
    """First truthy value for any of ``keys`` on a query-param/header source."""
    for key in keys:
        val = source.get(key)
        if val:
            return val
    return None


def _query_param_filters(
    q_params: Any,
) -> tuple[list[str], list[str], list[str], list[str], str | None]:
    """(enabled_tools, disabled_tools, enabled_tags, disabled_tags, query) from request query params."""
    return (
        _collect_multi_values(q_params, ["tools", "toolsets"]),
        _collect_multi_values(q_params, ["disabled_tools", "disabled_toolsets"]),
        _collect_multi_values(q_params, ["tags"]),
        _collect_multi_values(q_params, ["disabled_tags"]),
        _first_present(q_params, ["q", "query", "search"]),
    )


def _header_filters(
    headers: Any,
) -> tuple[list[str], list[str], list[str], list[str], str | None]:
    """(enabled_tools, disabled_tools, enabled_tags, disabled_tags, query) from request headers."""
    return (
        _collect_multi_values(
            headers, ["x-mcp-enabled-tools", "x-mcp-enabled-components"]
        ),
        _collect_multi_values(
            headers, ["x-mcp-disabled-tools", "x-mcp-disabled-components"]
        ),
        _collect_multi_values(headers, ["x-mcp-enabled-tags"]),
        _collect_multi_values(headers, ["x-mcp-disabled-tags"]),
        _first_present(headers, ["x-mcp-query", "x-mcp-search"]),
    )


def _apply_narrow_union_overrides(
    enabled_tools: list[str],
    disabled_tools: list[str],
    enabled_tags: list[str],
    disabled_tags: list[str],
    query_filter: str | None,
    extracted: tuple[list[str], list[str], list[str], list[str], str | None],
) -> tuple[list[str], list[str], list[str], list[str], str | None]:
    """Layer one extracted (query-param or header) filter set on top of the
    running lists: enabled/tags narrow, disabled/tags union, query replaces."""
    tools, disabled, tags, new_disabled_tags, query = extracted
    if tools:
        enabled_tools = _narrow_values(enabled_tools, _bounded_filter_values(tools))
    if disabled:
        disabled_tools = _union_values(disabled_tools, _bounded_filter_values(disabled))
    if tags:
        enabled_tags = _narrow_values(enabled_tags, _bounded_filter_values(tags))
    if new_disabled_tags:
        disabled_tags = _union_values(
            disabled_tags, _bounded_filter_values(new_disabled_tags)
        )
    if query:
        query_filter = query
    return enabled_tools, disabled_tools, enabled_tags, disabled_tags, query_filter


def _apply_request_overrides(
    enabled_tools: list[str],
    disabled_tools: list[str],
    enabled_tags: list[str],
    disabled_tags: list[str],
    query_filter: str | None,
) -> tuple[list[str], list[str], list[str], list[str], str | None, bool]:
    """Read the active HTTP request (query params, then headers) and layer
    its filters on top of the given lists. A malformed client filter must
    never widen the surface or silently fall back to the full component
    set, so any failure here rejects everything instead.

    Returns (enabled_tools, disabled_tools, enabled_tags, disabled_tags,
    query_filter, reject_all).
    """
    try:
        req = _optional_http_request()
        if not req:
            return (
                enabled_tools,
                disabled_tools,
                enabled_tags,
                disabled_tags,
                query_filter,
                False,
            )

        enabled_tools, disabled_tools, enabled_tags, disabled_tags, query_filter = (
            _apply_narrow_union_overrides(
                enabled_tools,
                disabled_tools,
                enabled_tags,
                disabled_tags,
                query_filter,
                _query_param_filters(req.query_params),
            )
        )
        if req.headers:
            enabled_tools, disabled_tools, enabled_tags, disabled_tags, query_filter = (
                _apply_narrow_union_overrides(
                    enabled_tools,
                    disabled_tools,
                    enabled_tags,
                    disabled_tags,
                    query_filter,
                    _header_filters(req.headers),
                )
            )
    except Exception as exc:
        logger.warning(
            "Rejected MCP visibility filter (exception_type=%s)",
            type(exc).__name__,
            exc_info=True,
        )
        return (
            enabled_tools,
            disabled_tools,
            enabled_tags,
            disabled_tags,
            query_filter,
            True,
        )

    return (
        enabled_tools,
        disabled_tools,
        enabled_tags,
        disabled_tags,
        query_filter,
        False,
    )


def _semantic_kg_matches(query_filter: str, name: str) -> list[str] | None:
    """Resolve a `q=` semantic filter through the active KG engine.

    Returns the matched tool names (possibly empty, meaning "no match"), or
    ``None`` if the engine/orchestrator import or resolution itself failed.
    """
    exc = not_served("knowledge_graph.core.engine", "MEDULLA-PLACEMENT-R611")
    logger.debug(
        "Failed to filter components using Knowledge Graph (exception_type=%s)",
        type(exc).__name__,
    )
    return None


def _apply_semantic_filter(
    query_filter: str | None, enabled_tools_list: list[str], name: str
) -> tuple[list[str], bool]:
    """A requested semantic filter is a least-privilege boundary: no match,
    no active graph, or any resolution failure exposes no components -- it
    must never widen back to the configured surface.

    Returns (enabled_tools_list, reject_all).
    """
    if not query_filter:
        return enabled_tools_list, False
    kg_matched = _semantic_kg_matches(query_filter, name)
    if not kg_matched:
        return enabled_tools_list, True
    if enabled_tools_list and "all" not in enabled_tools_list:
        narrowed = [tool for tool in enabled_tools_list if tool in kg_matched]
        return (narrowed, False) if narrowed else (narrowed, True)
    return kg_matched, False


def _finalize_filter_sets(
    enabled_tags_list: list[str],
    disabled_tags_list: list[str],
    enabled_tools_list: list[str],
    disabled_tools_list: list[str],
) -> tuple[set[str] | None, set[str] | None, set[str] | None, set[str] | None]:
    """Convert the resolved lists to sets, applying the "'all' or empty
    means expose everything" fallback to the enabled-tools set."""
    enabled_tags = set(enabled_tags_list) if enabled_tags_list else None
    disabled_tags = set(disabled_tags_list) if disabled_tags_list else None
    enabled_names = set(enabled_tools_list) if enabled_tools_list else None
    disabled_names = set(disabled_tools_list) if disabled_tools_list else None
    if enabled_names is not None and ("all" in enabled_names or not enabled_names):
        enabled_names = None
    return enabled_tags, disabled_tags, enabled_names, disabled_names


def _component_tag_visible(
    component: Any, enabled_tags: set[str] | None, disabled_tags: set[str] | None
) -> bool:
    """Tag-visibility rule for ONE already name-permitted component."""
    if hasattr(component, "tags") and component.tags:
        if enabled_tags is not None and not (component.tags & enabled_tags):
            return False
        return not (disabled_tags is not None and component.tags & disabled_tags)
    return enabled_tags is None


def _component_passes(
    component: Any,
    enabled_names: set[str] | None,
    disabled_names: set[str] | None,
    enabled_tags: set[str] | None,
    disabled_tags: set[str] | None,
) -> bool:
    """Whether ONE component survives the resolved name/tag filters.

    Fail closed (BUG-CX-022): a component carrying neither ``name`` nor ``uri``
    is EXCLUDED, not exempted. It used to return ``True`` here, skipping *all*
    visibility filtering -- name allow/deny lists and tag restrictions alike --
    so something that should have been hidden was handed to the caller.

    Exclusion (rather than raising) is the right refusal for this shape: the
    function answers one yes/no visibility question per component, and it is
    reached from ``DynamicVisibilityTransform._filter_components`` on the
    ``list_tools``/``list_resources``/``list_resource_templates`` path, where
    raising would turn one malformed entry into a failed listing for every
    caller. Every real FastMCP component (Tool, Resource, ResourceTemplate,
    Prompt) inherits a ``name``, so an object with neither identifier is not a
    component this server can vouch for exposing in the first place.
    """
    c_name = getattr(component, "name", None) or getattr(component, "uri", None)
    if not c_name:
        logger.warning(
            "Excluding an MCP component with neither name nor uri: it cannot be "
            "checked against the configured visibility filters"
        )
        return False
    if enabled_names is not None and c_name not in enabled_names:
        return False
    if disabled_names is not None and c_name in disabled_names:
        return False
    return _component_tag_visible(component, enabled_tags, disabled_tags)


def _optional_http_request() -> Any | None:
    """Return the active FastMCP HTTP request, if this is an HTTP invocation.

    FastMCP deliberately raises when ``list_tools`` is called in-process (for
    example by connector certification) or over stdio because neither path has
    an HTTP request context.  That expected absence is not a rejected client
    filter.  Every other exception still propagates to the fail-closed filter
    guard below.
    """
    from fastmcp.server.dependencies import get_http_request

    try:
        return get_http_request()
    except RuntimeError as exc:
        if exc.args == (_NO_HTTP_REQUEST,):
            return None
        raise
