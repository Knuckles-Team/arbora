"""The six served build-slot lease operations (ARBORA-CAPACITY-R754.1, R754.2).

``capacity.lease.request``, ``.start``, ``.renew``, ``.release``, ``.revoke`` and ``.get``
reach the engine's lease family through the ``capacity_leases`` operation port. The holder
is the verified caller: every handler derives the engine owner digest from
``context.caller.principal`` and the tenant from ``context.caller.tenant``, and no params
model has a field that could set or override either. The engine's owner guard refuses a
holder acting on another holder's lease; an engine refusal passes through with its code.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from arbora.api.contracts.capacity_lease import (
    LeaseFenceParams,
    LeaseGetParams,
    LeaseRequestParams,
    LeaseResult,
    LeaseRevokeParams,
    LeaseStatusResult,
)
from arbora.api.contracts.provisioning import opaque_principal
from arbora.api.ops._common import build_read_op, build_write_op
from arbora.api.ops.ports import port_of
from arbora.api.registry import Confirm, OpSpec, Surface

HOLDER_SCOPE = "capacity:lease"
READ_SCOPE = "capacity:read"
ADMIN_SCOPE = "capacity:admin"
#: The engine replays a request, start, renew and release by the request's idempotency key;
#: a repeated revoke is the engine's typed ``TRANSITION_REFUSED`` from a terminal state.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "capacity.lease.request": "medulla:AcquireCapacity",
        "capacity.lease.start": "medulla:StartCapacity",
        "capacity.lease.renew": "medulla:RenewCapacity",
        "capacity.lease.release": "medulla:ReleaseCapacity",
        "capacity.lease.revoke": "medulla:RevokeCapacity",
    }
)
_HANDLER = "arbora.api.ops.capacity_lease.handle_capacity_lease_"


def _holder(context: Any) -> tuple[str, str]:
    """The engine owner digest and tenant of the verified caller; never a parameter."""
    return opaque_principal(str(context.caller.principal)), str(context.caller.tenant)


async def handle_capacity_lease_request(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    owner, tenant = _holder(context)
    leases = port_of(context, "capacity_leases")
    return {
        "value": await leases.request(
            context, owner=owner, tenant=tenant, params=params
        )
    }


async def _mutate(
    context: Any, method: str, params: Mapping[str, Any]
) -> dict[str, Any]:
    owner, tenant = _holder(context)
    leases = port_of(context, "capacity_leases")
    return {
        "value": await leases.mutate(
            context, method, owner=owner, tenant=tenant, params=params
        )
    }


async def handle_capacity_lease_start(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _mutate(context, "StartCapacity", params)


async def handle_capacity_lease_renew(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _mutate(context, "RenewCapacity", params)


async def handle_capacity_lease_release(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _mutate(context, "ReleaseCapacity", params)


async def handle_capacity_lease_revoke(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    from arbora.api.domain.deployment.assembly import require_confirmed_plan

    require_confirmed_plan(op)
    leases = port_of(context, "capacity_leases")
    return {
        "value": await leases.revoke(
            context, tenant=str(context.caller.tenant), params=params
        )
    }


async def handle_capacity_lease_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    leases = port_of(context, "capacity_leases")
    return {
        "value": await leases.status(
            context,
            tenant=str(context.caller.tenant),
            lease_id=params["lease_id"],
        )
    }


_SURFACES = frozenset({Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE})


def _write(
    name: str, summary: str, example: str, params: Any, scope: str, **kw: Any
) -> OpSpec:
    return build_write_op(
        op_id=f"capacity.lease.{name}",
        summary=summary,
        examples=(example,),
        params=params,
        result=LeaseResult,
        handler=_HANDLER + name,
        scope=scope,
        surfaces=_SURFACES,
        **kw,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _write(
            "request",
            "Ask for one build slot of a pool",
            "request a build slot in this pool",
            LeaseRequestParams,
            HOLDER_SCOPE,
            confirm=Confirm.NONE,
        ),
        _write(
            "start",
            "Start work on a granted build slot lease",
            "start work on my granted build slot",
            LeaseFenceParams,
            HOLDER_SCOPE,
            confirm=Confirm.NONE,
        ),
        _write(
            "renew",
            "Renew a held build slot lease",
            "renew my build slot lease",
            LeaseFenceParams,
            HOLDER_SCOPE,
            confirm=Confirm.NONE,
        ),
        _write(
            "release",
            "Release a build slot lease",
            "release my build slot",
            LeaseFenceParams,
            HOLDER_SCOPE,
            confirm=Confirm.NONE,
        ),
        _write(
            "revoke",
            "Take a build slot lease back as an administrator",
            "revoke this build slot lease",
            LeaseRevokeParams,
            ADMIN_SCOPE,
            confirm=Confirm.PLAN,
        ),
        build_read_op(
            op_id="capacity.lease.get",
            summary="Show a build slot lease with its newest transitions",
            examples=("show this build slot lease",),
            params=LeaseGetParams,
            result=LeaseStatusResult,
            handler=_HANDLER + "get",
            scope=READ_SCOPE,
            surfaces=_SURFACES,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "handle_capacity_lease_get",
    "handle_capacity_lease_release",
    "handle_capacity_lease_renew",
    "handle_capacity_lease_request",
    "handle_capacity_lease_revoke",
    "handle_capacity_lease_start",
    "operations",
    "specs",
]
