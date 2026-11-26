"""Access and approval operations for the Arbora intent registry (MCPI-11)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

from arbora.access.service import (
    apply_ownership,
    approve_elevation,
    check_access,
    explain_policy,
    get_lease,
    grant_share,
    list_elevations,
    list_leases,
    plan_ownership,
    request_elevation,
    revoke_elevation,
    revoke_share,
)
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Surface,
    Verb,
)
from arbora.security.elevation import (
    ElevationApproval,
    ElevationRequest,
    ElevationRevocation,
    ElevationView,
    ElevationWriteResult,
)

#: Re-exported so ``registry_factory.get_registry()`` can resolve each
#: ``Composite(handler="arbora.api.ops.access.<name>")`` binding from this
#: module's own namespace, matching the ``agents``/``browser``/``fleet``
#: ops-module convention even though the implementations live in the
#: service layer (``arbora.access.service``).
__all__ = [
    "operations",
    "apply_ownership",
    "approve_elevation",
    "check_access",
    "explain_policy",
    "get_lease",
    "grant_share",
    "plan_ownership",
    "revoke_share",
    "list_elevations",
    "list_leases",
    "request_elevation",
    "revoke_elevation",
]


#: Share and ownership writes replay under the engine ``RbacAdmin`` key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "access.share.grant": "medulla:RbacAdmin",
    "access.share.revoke": "medulla:RbacAdmin",
    "access.ownership.apply": "medulla:RbacAdmin",
}


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(_Params):
    pass


class ElevationPage(_Params):
    elevations: list[ElevationView]


class LeaseList(_Params):
    kind: str | None = None
    status: Literal["active", "consumed", "revoked", "expired"] | None = None
    cursor: str | None = None
    limit: int = Field(default=100, ge=1, le=100)


class LeaseGet(_Params):
    lease_id: str = Field(min_length=1, max_length=256)


class AccessCheck(_Params):
    graph: str = Field(min_length=1, max_length=512)
    agent_id: str = Field(min_length=1, max_length=256)
    action: Literal["read", "write"] = "read"


class AccessDecision(_Params):
    allowed: bool


class PolicyExplain(_Params):
    plan: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class ShareParams(_Params):
    node_id: str = Field(min_length=1, max_length=512)
    scope: Literal["private", "placement", "tenant"]
    grantees: list[str] = Field(default_factory=list, max_length=64)


class ShareRevokeParams(_Params):
    node_id: str = Field(min_length=1, max_length=512)


class OwnershipPlanParams(_Params):
    graph: str | None = Field(default=None, min_length=1, max_length=512)


class OwnershipApplyParams(_Params):
    graph: str = Field(min_length=1, max_length=512)
    plan: list[dict[str, Any]] = Field(max_length=10_000)
    plan_digest: str = Field(min_length=1, max_length=128)
    signals_digest: str = Field(min_length=1, max_length=128)


class JsonResult(RootModel[dict[str, Any]]):
    pass


_ALL = frozenset({Surface.MCP, Surface.HTTP, Surface.A2A})
_CONSOLE = frozenset({Surface.MCP, Surface.HTTP, Surface.A2A, Surface.CONSOLE})


def _op(
    name: str,
    verb: Verb,
    summary: str,
    params: type[BaseModel],
    result: type[BaseModel],
    *,
    scopes: tuple[str, ...],
    effect: Effect = Effect.READ,
    principals: PrincipalRule = PrincipalRule.ANY,
    confirm: Confirm | None = None,
    surfaces: frozenset[Surface] = _ALL,
    idempotency: Idempotency = Idempotency.NONE,
) -> OpSpec:
    handler = {
        "access.elevation.request": "request_elevation",
        "access.elevation.list": "list_elevations",
        "access.elevation.revoke": "revoke_elevation",
        "access.elevation.approve": "approve_elevation",
        "access.leases.list": "list_leases",
        "access.leases.get": "get_lease",
        "access.check": "check_access",
        "access.explain_policy": "explain_policy",
        "access.share.grant": "grant_share",
        "access.share.revoke": "revoke_share",
        "access.ownership.plan": "plan_ownership",
        "access.ownership.apply": "apply_ownership",
    }[name]
    return OpSpec(
        id=name,
        verb=verb,
        summary=summary,
        examples=(summary,),
        params=params,
        result=result,
        binding=Composite(handler=f"arbora.api.ops.access.{handler}"),
        scopes=frozenset(scopes),
        effect=effect,
        principals=principals,
        confirm=confirm,
        surfaces=surfaces,
        idempotency=idempotency,
        audit=AuditClass.EVENT if effect is not Effect.READ else AuditClass.NONE,
    )


def operations() -> tuple[OpSpec, ...]:
    """Supported access operations; native EG deny is tracked as a gap.

    ``approvals.list``/``get``/``grant``/``deny`` are implemented in
    :mod:`arbora.access.service` but intentionally not registered here:
    the installed EG contract does not yet publish the ``approvals:read``/
    ``approvals:decide`` scopes they require (ARBORA-IDENTITY-R019 follow-up,
    tracked in ``specs/identity-access/tasks.md``).
    """
    return (
        _op(
            "access.elevation.request",
            Verb.MANAGE,
            "Request a time-boxed elevation",
            ElevationRequest,
            ElevationWriteResult,
            scopes=("rbac:elevation",),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
        ),
        _op(
            "access.elevation.list",
            Verb.ASK,
            "List visible elevation requests",
            Empty,
            ElevationPage,
            scopes=("rbac:elevation-read",),
        ),
        _op(
            "access.elevation.revoke",
            Verb.MANAGE,
            "Revoke an elevation",
            ElevationRevocation,
            ElevationWriteResult,
            scopes=("rbac:elevation",),
            effect=Effect.DESTRUCTIVE,
        ),
        _op(
            "access.elevation.approve",
            Verb.MANAGE,
            "Approve an exact elevation request",
            ElevationApproval,
            ElevationWriteResult,
            scopes=("rbac:approve-elevation",),
            effect=Effect.ADMIN,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
            confirm=Confirm.CONSOLE,
            surfaces=_CONSOLE,
        ),
        _op(
            "access.leases.list",
            Verb.ASK,
            "List tenant control leases",
            LeaseList,
            JsonResult,
            scopes=("lease:read",),
        ),
        _op(
            "access.leases.get",
            Verb.ASK,
            "Get a tenant control lease",
            LeaseGet,
            JsonResult,
            scopes=("lease:read",),
        ),
        _op(
            "access.check",
            Verb.WHY,
            "Check current graph access",
            AccessCheck,
            AccessDecision,
            scopes=("security:check",),
        ),
        _op(
            "access.explain_policy",
            Verb.WHY,
            "Explain RLS policy for a query plan",
            PolicyExplain,
            JsonResult,
            scopes=("explain:read",),
        ),
        _op(
            "access.share.grant",
            Verb.MANAGE,
            "Share a node with named grantees at one scope",
            ShareParams,
            JsonResult,
            scopes=("access:share",),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
        ),
        _op(
            "access.share.revoke",
            Verb.MANAGE,
            "Revoke a node's share",
            ShareRevokeParams,
            JsonResult,
            scopes=("access:share",),
            effect=Effect.WRITE,
            idempotency=Idempotency.KEY_REQUIRED,
        ),
        _op(
            "access.ownership.plan",
            Verb.ASK,
            "Plan graph ownership from the engine's signals",
            OwnershipPlanParams,
            JsonResult,
            scopes=("access:ownership",),
        ),
        _op(
            "access.ownership.apply",
            Verb.MANAGE,
            "Apply an approved graph ownership plan",
            OwnershipApplyParams,
            JsonResult,
            scopes=("access:ownership",),
            effect=Effect.ADMIN,
            principals=PrincipalRule.HUMAN_UNDELEGATED,
            confirm=Confirm.PLAN,
            surfaces=_CONSOLE,
        ),
    )
