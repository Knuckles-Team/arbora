"""Caller-filtered operation and fleet discovery for the MCP ``find`` verb."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel

FleetSearch = Callable[..., Awaitable[Sequence[Mapping[str, Any]]]]
FleetCatalog = Callable[..., Awaitable[Mapping[str, Any]]]

#: Retry hint for a caller refused while the fleet comes up (ARBORA-FLEET-R062).
FLEET_RETRY_AFTER_S = 5


def not_ready_detail() -> dict[str, Any]:
    """The typed refusal detail while the fleet gateway is not bound."""

    return {
        "reason": "fleet gateway is not ready",
        "retry_after_s": FLEET_RETRY_AFTER_S,
    }


#: The find op that browses one fleet server's catalog (ARBORA-FLEET-R063).
CATALOG_OP = "catalog"


def discovery_budget_s() -> float:
    """The interactive fleet discovery budget (``MCP_DYNAMIC_DISCOVERY_TIMEOUT``)."""

    from arbora.core.settings import runtime_config

    return float(getattr(runtime_config, "mcp_dynamic_discovery_timeout", 5.0))


def _fleet_not_ready(exc: BaseException) -> bool:
    """True for the late gateway's refusal before the fleet is bound."""

    return getattr(exc, "code", None) == "UNAVAILABLE"


async def visible_ops(
    registry: Any, caller: Any, policy_gate: Any, *, verb: str | None = None
) -> tuple[Any, ...]:
    """Batch caller-scoped PDP decisions in registry order."""

    from arbora.api.policy import PolicyUnavailable, op_resource
    from arbora.api.registry import Surface

    candidates = tuple(
        item
        for item in registry
        if Surface.MCP in item.surfaces and (verb is None or item.verb.value == verb)
    )
    try:
        decisions = await policy_gate.visible(
            [op_resource(item) for item in candidates], caller
        )
    except Exception as exc:
        raise PolicyUnavailable("policy decision point unavailable") from exc
    if len(decisions) != len(candidates):
        raise PolicyUnavailable("policy response alignment failed")
    return tuple(
        item
        for item, allowed in zip(candidates, decisions, strict=True)
        if allowed is True
    )


def _schema(model: Any) -> dict[str, Any]:
    if isinstance(model, type) and issubclass(model, BaseModel):
        return model.model_json_schema()
    return {"$ref": model.path} if hasattr(model, "path") else {}


def describe_op(op: Any) -> dict[str, Any]:
    """Expose a complete typed descriptor from a visible registry entry."""

    return {
        "kind": "op",
        "op": op.id,
        "verb": op.verb.value,
        "summary": op.summary,
        "params_schema": _schema(op.params),
        "result_schema": _schema(op.result),
        "scopes": sorted(op.scopes),
        "authorized": True,
        "effect": op.effect.value,
        "confirm": op.confirm.value,
        "stability": op.stability.value,
        "examples": list(op.examples),
    }


def _fleet_descriptor(item: Mapping[str, Any]) -> dict[str, Any]:
    """Preserve a fleet result while identifying its dispatch route."""

    return {
        **item,
        "kind": "fleet",
        "verb": "act",
        "op": "fleet.call",
        "authorized": True,
    }


def _matching_ops(
    visible: Iterable[Any], op: str | None, domain: Any
) -> tuple[Any, ...]:
    """Filter visible ops to an exact id and, if given, a dotted domain prefix."""

    selected = tuple(item for item in visible if op is None or item.id == op)
    if domain is None:
        return selected
    return tuple(item for item in selected if item.id.startswith(f"{domain}."))


async def _fleet_entries(
    fleet_search: FleetSearch,
    *,
    caller: Any,
    intent: str | None,
    params: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Run the caller-filtered fleet catalog search for unmatched discovery.

    Bounded by the discovery budget (ARBORA-FLEET-R062): a slow or unbound
    fleet raises :class:`FleetPartial` so the caller keeps its registry
    results and flags the fleet half partial.
    """

    import asyncio

    try:
        fleet = await asyncio.wait_for(
            fleet_search(
                caller=caller,
                query=intent or "",
                context_budget_tokens=(params or {}).get("context_budget_tokens"),
            ),
            timeout=discovery_budget_s(),
        )
    except TimeoutError as exc:
        raise FleetPartial("timeout") from exc
    except Exception as exc:
        if _fleet_not_ready(exc):
            raise FleetPartial("not_ready") from exc
        raise
    return [_fleet_descriptor(item) for item in fleet]


class FleetPartial(Exception):
    """The fleet half of discovery did not answer; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _ranked_entries(
    entries: list[dict[str, Any]],
    *,
    resolver: Any,
    intent: str,
    scope_ref: str | None,
) -> list[dict[str, Any]]:
    """Reorder merged op and fleet entries by the resolver's own ranking."""

    routing = [
        {**item, "op": item["id"]}
        if item["kind"] == "fleet" and item.get("id")
        else item
        for item in entries
    ]
    ranked = resolver.rank("find", intent, routing, scope_ref=scope_ref, top_k=20)
    by_id = {str(item.get("id") or item["op"]): item for item in entries}
    return [by_id[item.descriptor.id] for item in ranked if item.descriptor.id in by_id]


def _bounded_limit(params: Mapping[str, Any] | None) -> int:
    limit = (params or {}).get("limit", 20)
    return max(1, min(limit, 100)) if isinstance(limit, int) else 20


async def find_visible(
    *,
    registry: Any,
    caller: Any,
    policy_gate: Any,
    resolver: Any,
    fleet_search: FleetSearch | None,
    fleet_catalog: FleetCatalog | None = None,
    intent: str | None = None,
    op: str | None = None,
    params: Mapping[str, Any] | None = None,
    scope_ref: str | None = None,
) -> dict[str, Any]:
    """Search only the caller's authorized operations and fleet items.

    ``fleet_search`` must apply its own scope, principal and Eunomia filter;
    unchecked child catalog rows are never accepted by this API.
    """

    if op == CATALOG_OP and registry.get(op) is None:
        return await _fleet_catalog(fleet_catalog, caller, params, registry)
    visible = await visible_ops(registry, caller, policy_gate)
    selected = _matching_ops(visible, op, (params or {}).get("domain"))
    entries = [describe_op(item) for item in selected]
    partial: str | None = None
    if op is None and fleet_search is not None:
        try:
            entries.extend(
                await _fleet_entries(
                    fleet_search, caller=caller, intent=intent, params=params
                )
            )
        except FleetPartial as exc:
            partial = exc.reason
    if intent and op is None:
        entries = _ranked_entries(
            entries, resolver=resolver, intent=intent, scope_ref=scope_ref
        )
    found: dict[str, Any] = {
        "items": entries[: _bounded_limit(params)],
        "registry_digest": registry.digest,
        "next_cursor": None,
    }
    if partial is not None:
        found["fleet_partial"] = True
        found["fleet"] = partial
    return found


async def _fleet_catalog(
    fleet_catalog: FleetCatalog | None,
    caller: Any,
    params: Mapping[str, Any] | None,
    registry: Any,
) -> dict[str, Any]:
    """``find(op="catalog")``: one server's catalog from the multiplexer handler."""

    from arbora.api.invoke.pipeline import OperationRefused

    if fleet_catalog is None:
        raise OperationRefused("UNAVAILABLE", not_ready_detail())
    server = str((params or {}).get("server") or "")[:128]
    include_tools = bool((params or {}).get("include_tools", True))
    from arbora.fleet.multiplexer_ops import FleetServerNotFound

    try:
        catalog = await fleet_catalog(
            caller=caller, server=server, include_tools=include_tools
        )
    except FleetServerNotFound as missing:
        raise OperationRefused(
            "NOT_FOUND",
            {"server": missing.server, "pack_status": missing.pack_status},
        ) from None
    return {"catalog": dict(catalog), "registry_digest": registry.digest}
