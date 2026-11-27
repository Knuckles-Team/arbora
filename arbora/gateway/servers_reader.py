"""The typed fleet-catalog ``servers`` reader (ARBORA-FLEET-R033.1, R033.2).

``registry_api`` kind ``servers`` reads the ``mcp_servers`` table written by
the fleet catalog tables through this one reader. The reader checks the
caller before any read and returns :class:`RegistryServer` records scoped to
the caller's tenant. It writes nothing. A refusal carries a typed code and no
record, so a refused read cannot tell a foreign object from an absent one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

ServersRefusalCode = Literal["PRINCIPAL_REQUIRED", "TENANT_MISMATCH", "SCOPE_DENIED"]

#: The scope a registry read needs (the same scope the other kinds require).
REQUIRED_SCOPE = "kg:read"


class ServersRefused(Exception):
    """The caller may not read the servers catalog; carries one typed code."""

    def __init__(self, code: ServersRefusalCode) -> None:
        super().__init__(code)
        self.code: ServersRefusalCode = code


@dataclass(frozen=True, slots=True)
class ServersCaller:
    """The verified caller of one read.

    ``requested_tenant`` is the tenant the request names, when it names one.
    """

    tenant: str
    principal: str
    scopes: frozenset[str]
    requested_tenant: str | None = None


@dataclass(frozen=True, slots=True)
class ServersPageRequest:
    limit: int
    query: str = ""
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ServersPage:
    items: list[Any]
    count: int
    next_cursor: str | None


class FleetCatalogServersReader:
    """Reads the ``mcp_servers`` table for one verified caller."""

    def check(self, caller: ServersCaller) -> None:
        """Refuse before any read; the order is principal, tenant, scope."""
        if not caller.principal or not caller.tenant:
            raise ServersRefused("PRINCIPAL_REQUIRED")
        if caller.requested_tenant not in (None, "", caller.tenant):
            raise ServersRefused("TENANT_MISMATCH")
        if REQUIRED_SCOPE not in caller.scopes:
            raise ServersRefused("SCOPE_DENIED")

    async def list(
        self, caller: ServersCaller, page: ServersPageRequest
    ) -> ServersPage:
        """One keyset page of the servers visible to ``caller``."""
        from arbora.gateway import registry_api as api

        self.check(caller)
        tenant, principal = caller.tenant, caller.principal
        after = None
        if page.cursor:
            after = api._decode_cursor(
                page.cursor,
                kind="servers",
                query=page.query,
                tenant=tenant,
                principal=principal,
                grant_digests=(),
            )
        spec = api._KIND_SPECS["servers"]
        engine = api._get_catalog_engine()
        rows = await api._offload_catalog_call(
            api._authorized_page,
            "servers",
            tenant=tenant,
            principal=principal,
            grant_digests=(),
            query=page.query,
            after=after,
            limit=page.limit,
            engine=engine,
        )
        has_more = len(rows) > page.limit
        page_rows = rows[: page.limit]
        total = await api._page_total(
            page_rows,
            has_more=has_more,
            after=after,
            count=lambda: api._offload_catalog_call(
                api._authorized_count,
                "servers",
                tenant=tenant,
                principal=principal,
                grant_digests=(),
                query=page.query,
                engine=engine,
            ),
        )
        next_cursor = api._next_page_cursor(
            spec,
            page_rows,
            has_more,
            kind="servers",
            query=page.query,
            authority=(tenant, principal, ()),
        )
        items = [api._validate_item("servers", spec.model, row) for row in page_rows]
        return ServersPage(items=items, count=total, next_cursor=next_cursor)


__all__ = [
    "REQUIRED_SCOPE",
    "FleetCatalogServersReader",
    "ServersCaller",
    "ServersPage",
    "ServersPageRequest",
    "ServersRefused",
]
