"""The trusted declarations and caller grants behind workflow admission.

ARBORA-FLEET-R034.8, R034.9 and R021.1. The admission gate checks every tool of a
workflow against a trusted schema digest and the caller's grant. Both come from
the one verified fleet catalog the host already serves; arbora holds no second
declaration store and no grant table.

- ``declarations(caller)``: the tool name to the engine-recorded definition digest
  of every tool in the caller tenant's catalog.
- ``grants(caller)``: the tools the tenant's catalog serves and a revision that
  changes when the catalog or the caller's scopes change, so a stale binding is
  refused. The per-tool decision stays the one ``PolicyGate`` narrowing, which the
  gate runs before it reads grants (ARBORA-FLEET-R019.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from arbora.api.domain.fleet.admission_models import AdmissionRefused, canonical_digest

__all__ = ["CatalogAdmissionSources", "tool_name"]


def tool_name(server_name: str, upstream_name: str) -> str:
    """The name a workflow binds a catalog tool by: the multiplexer's served name."""
    from arbora.fleet.child_mounts import clean_tool_name, get_server_prefix

    return clean_tool_name(get_server_prefix(server_name), server_name, upstream_name)


class CatalogAdmissionSources:
    """Declarations and grants read from the verified fleet catalog of the caller."""

    def __init__(self, gateway: Any) -> None:
        self._gateway = gateway

    async def _tools(self, caller: Any) -> tuple[Any, dict[str, str]]:
        catalog = await self._gateway.read_catalog(caller)
        tenant = str(getattr(caller, "tenant", ""))
        if getattr(catalog.context, "tenant_id", None) != tenant:
            raise AdmissionRefused(
                "WORKFLOW_NOT_ADMITTED", {"reason": "no catalog for this caller"}
            )
        tools = {
            tool_name(item.entry.server_name, item.entry.upstream_name): (
                item.entry.definition_digest
            )
            for server in catalog.servers
            for item in server.provides
            if item.entry.kind == "tool"
        }
        return catalog, tools

    async def declarations(self, caller: Any) -> Mapping[str, str]:
        """Tool name to trusted schema digest for the caller's tenant."""
        _catalog, tools = await self._tools(caller)
        return tools

    async def grants(self, caller: Any) -> Mapping[str, Any]:
        """The served tools and the grant revision of the caller's current rights."""
        catalog, tools = await self._tools(caller)
        revision = canonical_digest(
            {
                "tenant": str(getattr(caller, "tenant", "")),
                "scopes": sorted(getattr(caller, "effective_scopes", ()) or ()),
                "catalog": sorted(tools.items()),
                "generation": getattr(catalog.generation, "digest", None),
            }
        )
        return {"revision": revision, "tools": sorted(tools)}
