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
from typing import Any

from pydantic import Field

from arbora.api.ops._common import (
    Params,
    bound_service,
    build_read_op,
    build_write_op,
)
from arbora.api.registry import OpSpec, Verb

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


def _port(context: Any) -> Any:
    return bound_service(
        context,
        "federation_sources",
        reason="federation source catalog is not composed",
    )


def _can_see(record: Mapping[str, Any], principal: str) -> bool:
    return principal == record.get("owner") or principal in record.get("grantees", ())


async def _visible(context: Any, source_id: str) -> tuple[Any, dict[str, Any]]:
    port = _port(context)
    record = await port.get(source_id, tenant=context.caller.tenant)
    if record is None or not _can_see(record, context.caller.principal):
        raise LookupError("federation source not found")
    return port, dict(record)


def _public(record: Mapping[str, Any]) -> dict[str, Any]:
    return redact(dict(record))


async def handle_federation(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    principal = context.caller.principal
    if op.id == "federation.sources.list":
        records = await _port(context).list(tenant=context.caller.tenant)
        return {
            "value": {
                "sources": [_public(r) for r in records if _can_see(r, principal)]
            }
        }
    _, record = await _visible(context, params["source_id"])
    if op.id == "federation.sources.get":
        return {"value": _public(record)}
    if op.id == "federation.sources.probe":
        port = _port(context)
        return {
            "value": _public(
                await port.probe(params["source_id"], tenant=context.caller.tenant)
            )
        }
    if record.get("owner") != principal:
        raise PermissionError("only the owner can change who a source is shared with")
    if not context.idempotency_key:
        raise ValueError("Idempotency-Key is required")
    port = _port(context)
    if op.id == "federation.sources.share":
        updated = await port.grant(
            params["source_id"], params["grantee"], tenant=context.caller.tenant
        )
    elif op.id == "federation.sources.unshare":
        updated = await port.revoke(
            params["source_id"], params["grantee"], tenant=context.caller.tenant
        )
    else:
        raise ValueError("unknown federation operation")
    return {"value": _public(updated)}


def operations() -> tuple[OpSpec, ...]:
    handler = "arbora.api.ops.federation.handle_federation"
    return (
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
