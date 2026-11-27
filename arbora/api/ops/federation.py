"""Federation source list, get, probe, share and unshare (ARBORA-OPS-R019.1/.2).

Registration and column reads pass straight through to the engine
(:mod:`arbora.api.ops.query_engine`).  The visibility rule is Arbora's: a
source is visible only to its owner and its grantees, only the owner shares or
unshares it, and a probe re-checks that access on every call, so revoking a
grantee blocks that grantee's next probe.  Probe results never carry
credentials.  The durable record is held by the composed
``federation_sources`` port; an uncomposed port fails closed with UNAVAILABLE.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from pydantic import Field

from arbora.api.ops._common import (
    Params,
    bound_service,
    build_read_op,
    build_write_op,
)
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import OpSpec, Verb
from arbora.api.registry.spec import SOURCE_AUTHORITY

_SECRET_MARKERS = (
    "password",
    "secret",
    "token",
    "credential",
    "apikey",
    "api_key",
    "authorization",
    "dsn",
    "private_key",
)
REDACTED = "[redacted]"


class SourceRefParams(Params):
    source_id: str = Field(min_length=1, max_length=256)


class SourcesListParams(Params):
    pass


class SourceGrantParams(Params):
    source_id: str = Field(min_length=1, max_length=256)
    grantee: str = Field(min_length=1, max_length=256)


class SourceResult(Params):
    value: dict[str, Any]


class SupplyChainImpactParams(Params):
    """The ontology class roots the supply-chain question reads over."""

    class_roots: list[str] = Field(min_length=1, max_length=64)


class SupplyChainImpactResult(Params):
    """The engine's report, unchanged, with its sources and abstentions."""

    report: dict[str, Any]
    sources_used: list[Any]
    abstentions: list[Any]
    audit: list[Any] | None = None


def redact(value: Any) -> Any:
    """Return ``value`` with every credential-like field replaced."""
    if isinstance(value, Mapping):
        return {
            key: REDACTED
            if any(marker in str(key).lower() for marker in _SECRET_MARKERS)
            else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact(item) for item in value]
    return value


class EngineForeignSources:
    """The ``federation_sources`` port over the engine's foreign-source catalog.

    The durable record, its owner and its grantees live in medulla (ARBORA-OPS-R019);
    Arbora holds no catalog. Each request binds the port to the caller's own session
    (:meth:`bind`), and the engine answers a source the caller may not see exactly as
    an absent one, so ``engine_authorized`` tells the handlers not to re-decide
    visibility. PIN-LAG: a pinned engine without the methods refuses ``UNAVAILABLE``
    naming the first missing one.
    """

    def bind(self, context: Any) -> _BoundForeignSources:
        return _BoundForeignSources(context)


class _BoundForeignSources:
    @staticmethod
    def sees(record: Mapping[str, Any], principal: str) -> bool:
        return True

    @staticmethod
    def owns(record: Mapping[str, Any], principal: str) -> bool:
        return True

    def __init__(self, context: Any) -> None:
        self._context = context

    async def _send(self, method: str, params: dict[str, Any], **kw: Any) -> Any:
        from arbora.api.ops.pin_guards import send_main

        return await send_main(self._context, method, params, **kw)

    @staticmethod
    def _record(name: str, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump(mode="json")
        record = dict(value) if isinstance(value, Mapping) else {}
        return {"source_id": record.get("name", name), **record}

    async def get(self, source_id: str, *, tenant: str) -> dict[str, Any] | None:
        return self._record(
            source_id, await self._send("GetForeignSource", {"name": source_id})
        )

    async def list(self, *, tenant: str) -> list[dict[str, Any]]:
        listed = await self._send("ListForeignSources", {})
        if hasattr(listed, "model_dump"):
            listed = listed.model_dump(mode="json")
        rows = listed.get("sources", listed) if isinstance(listed, Mapping) else listed
        return [self._record("", row) for row in rows or ()]

    async def probe(self, source_id: str, *, tenant: str) -> dict[str, Any]:
        return self._record(
            source_id, await self._send("ProbeForeignSource", {"name": source_id})
        )

    async def grant(
        self, source_id: str, grantee: str, *, tenant: str
    ) -> dict[str, Any]:
        return self._record(
            source_id,
            await self._send(
                "ShareForeignSource",
                {"name": source_id, "grantees": [grantee]},
                idempotent=True,
            ),
        )

    async def revoke(
        self, source_id: str, grantee: str, *, tenant: str
    ) -> dict[str, Any]:
        return self._record(
            source_id,
            await self._send(
                "UnshareForeignSource",
                {"name": source_id, "grantees": [grantee]},
                idempotent=True,
            ),
        )


def _port(context: Any) -> Any:
    port = bound_service(
        context,
        "federation_sources",
        reason="federation source catalog is not composed",
    )
    return getattr(port, "bind", lambda _context: port)(context)


def _owner_or_grantee(record: Mapping[str, Any], principal: str) -> bool:
    return principal == record.get("owner") or principal in record.get("grantees", ())


def _is_owner(record: Mapping[str, Any], principal: str) -> bool:
    return record.get("owner") == principal


def _can_see(record: Mapping[str, Any], principal: str, port: Any) -> bool:
    """The engine port decides visibility itself; a plain catalog port is Arbora's rule."""
    return getattr(port, "sees", _owner_or_grantee)(record, principal)


async def _visible(context: Any, source_id: str) -> tuple[Any, dict[str, Any]]:
    port = _port(context)
    record = await port.get(source_id, tenant=context.caller.tenant)
    if record is None or not _can_see(record, context.caller.principal, port):
        raise LookupError("federation source not found")
    return port, dict(record)


def _public(record: Mapping[str, Any]) -> dict[str, Any]:
    return redact(dict(record))


async def _list(context: Any, params: Mapping[str, Any], op: OpSpec) -> dict[str, Any]:
    port = _port(context)
    principal = context.caller.principal
    records = await port.list(tenant=context.caller.tenant)
    sources = [_public(r) for r in records if _can_see(r, principal, port)]
    return {"value": {"sources": sources}}


async def _get(context: Any, params: Mapping[str, Any], op: OpSpec) -> dict[str, Any]:
    _, record = await _visible(context, params["source_id"])
    return {"value": _public(record)}


async def _probe(context: Any, params: Mapping[str, Any], op: OpSpec) -> dict[str, Any]:
    port, _ = await _visible(context, params["source_id"])
    probed = await port.probe(params["source_id"], tenant=context.caller.tenant)
    return {"value": _public(probed)}


async def _change(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    port, record = await _visible(context, params["source_id"])
    if not getattr(port, "owns", _is_owner)(record, context.caller.principal):
        raise PermissionError("only the owner can change who a source is shared with")
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    change = port.grant if op.id == "federation.sources.share" else port.revoke
    updated = await change(
        params["source_id"], params["grantee"], tenant=context.caller.tenant
    )
    return {"value": _public(updated)}


_HANDLERS = MappingProxyType(
    {
        "federation.sources.list": _list,
        "federation.sources.get": _get,
        "federation.sources.probe": _probe,
        "federation.sources.share": _change,
        "federation.sources.unshare": _change,
    }
)


async def handle_federation(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _HANDLERS[op.id](context, params, op)


async def connected_sources(tenant: str, *, catalog: Any) -> tuple[str, ...]:
    """The tenant's admitted connectors, from the verified fleet catalog.

    ``catalog`` is the verified snapshot of ``FleetCatalogReader.read``. A
    snapshot bound to another tenant is never read (ARBORA-FLEET-R052.2).
    """
    if getattr(catalog.context, "tenant_id", None) != tenant:
        raise PermissionError("fleet catalog belongs to another tenant")
    return tuple(sorted({server.component.server_name for server in catalog.servers}))


async def handle_supply_chain_impact(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve the engine's supply-chain impact report through one operation.

    Arbora passes the connected connectors in the request and the engine's
    report back unchanged; abstentions (an authoritative source with no
    connected connector comes back as ``Unconnected``) stay the engine's.
    """
    gateway = bound_service(
        context, "fleet_gateway", reason="fleet catalog is not composed"
    )
    tenant = str(context.caller.tenant)
    sources = await connected_sources(
        tenant, catalog=await gateway.read_catalog(context.caller)
    )
    report = await engine_send(
        context,
        "SupplyChainImpact",
        {
            "class_roots": list(params["class_roots"]),
            "connected_sources": list(sources),
        },
    )
    if not isinstance(report, Mapping):
        raise unavailable("engine result malformed")
    return {
        "report": dict(report),
        "sources_used": list(report.get("sources_used", ())),
        "abstentions": list(report.get("abstentions", ())),
        # The engine's sources-used-and-why block, unaltered (ARBORA-FLEET-R052.3).
        "audit": report.get("audit"),
    }


def operations() -> tuple[OpSpec, ...]:
    handler = "arbora.api.ops.federation.handle_federation"
    return (
        build_read_op(
            op_id="report.supply_chain_impact",
            summary="Report which services a supplier or vulnerability impacts",
            examples=("which services does this vulnerability impact",),
            params=SupplyChainImpactParams,
            result=SupplyChainImpactResult,
            handler="arbora.api.ops.federation.handle_supply_chain_impact",
            scope="federation:read",
            tags=frozenset({SOURCE_AUTHORITY}),
        ),
        build_read_op(
            op_id="federation.sources.list",
            summary="List the federation sources visible to the caller",
            examples=("list my federated sources",),
            params=SourcesListParams,
            result=SourceResult,
            handler=handler,
            scope="federation:read",
            verb=Verb.FIND,
        ),
        build_read_op(
            op_id="federation.sources.get",
            summary="Get one visible federation source",
            examples=("show this federated source",),
            params=SourceRefParams,
            result=SourceResult,
            handler=handler,
            scope="federation:read",
        ),
        build_read_op(
            op_id="federation.sources.probe",
            summary="Probe one visible federation source; credentials are redacted",
            examples=("probe this federated source",),
            params=SourceRefParams,
            result=SourceResult,
            handler=handler,
            scope="federation:read",
        ),
        build_write_op(
            op_id="federation.sources.share",
            summary="Share an owned federation source with a grantee",
            examples=("share this federated source",),
            params=SourceGrantParams,
            result=SourceResult,
            handler=handler,
            scope="federation:admin",
        ),
        build_write_op(
            op_id="federation.sources.unshare",
            summary="Revoke a grantee's access to an owned federation source",
            examples=("unshare this federated source",),
            params=SourceGrantParams,
            result=SourceResult,
            handler=handler,
            scope="federation:admin",
        ),
    )


specs = operations

#: The engine replays a share or unshare by the request key.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "federation.sources.share": "medulla:ShareForeignSource",
        "federation.sources.unshare": "medulla:UnshareForeignSource",
    }
)
