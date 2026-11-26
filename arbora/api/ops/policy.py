"""Policy read operation (ARBORA-OPS-R021.3, policy slice).

``policy.read`` hands back the caller's own tenant's active policy state
from the composed ``policy_reader`` port. The OpSpec wiring, the
not-composed-yet refusal, and the trivial params/result pair all come from
:mod:`arbora.api.ops._common`; this module only supplies the port
protocol, the state shape, and the service name.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send
from arbora.api.registry import Confirm, Effect, OpSpec
from arbora.deployment.policy_bundle import BundleRefused, load_bundle
from arbora.pin_lag.medulla import PinLagUnavailable, refusal, require_models

PolicyReadParams, PolicyReadResult = _common.read_op_models("PolicyRead")


class PolicyState(_common.TenantState):
    """The active, tenant-scoped policy state; the reader owns its exact shape."""

    rules: dict[str, str] = Field(default_factory=dict)


@runtime_checkable
class PolicyReader(Protocol):
    """The one typed port Arbora calls the active policy state through."""

    async def read(self, *, tenant: str) -> PolicyState: ...


async def handle_policy_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return this tenant's active policy state through the composed reader."""
    return await _common.handle_tenant_read(
        context,
        service_name="policy_reader",
        unavailable_reason="policy reader is not composed",
    )


class BundleParams(_common.Params):
    name: str = Field(min_length=1, max_length=64)


class BundleResult(_common.Params):
    reply: dict[str, Any]


def _bundle_root(context: Any) -> Any:
    from pathlib import Path

    from arbora.core.settings import config_dir

    root = context.services.get("policy_bundle_root")
    return Path(root) if root is not None else config_dir() / "policy"


async def _send_bundle(
    context: Any, params: Mapping[str, Any], verb: str
) -> dict[str, Any]:
    """Send the file's bundle unchanged to the engine policy family; refusals pass through."""
    try:
        # PIN-LAG: the pinned engine has no identity policy family yet.
        require_models(("IdentityOpPolicy",))
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    try:
        bundle = load_bundle(_bundle_root(context), params["name"])
    except BundleRefused as error:
        raise OperationRefused(error.code, error.details) from error
    reply = await engine_send(
        context,
        "Identity",
        {"op": {"family": "policy", "op": verb, "bundle": bundle.wire()}},
        idempotent=verb == "bind",
    )
    return {"reply": dict(reply) if isinstance(reply, Mapping) else {"value": reply}}


async def handle_bundle_validate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Compile the bundle in the engine without writing."""
    return await _send_bundle(context, params, "validate")


async def handle_bundle_bind(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Bind the bundle to its scope; the engine enforces it in its one access boundary."""
    return await _send_bundle(context, params, "bind")


class PolicyReviewParams(_common.Params):
    repository: str = Field(min_length=1, max_length=256)
    candidate_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_blob_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    invariant_generation: int = Field(ge=0)
    #: The principal that authored the revision; the reviewer must be another.
    author: str = Field(min_length=1, max_length=256)


class PolicyReviewResult(_common.Params):
    value: dict[str, Any]


async def handle_policy_review(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Return a ``ReviewApproval`` bound to the digests and the verified reviewer."""
    reviewer = str(context.caller.principal)
    if reviewer == params["author"]:
        raise OperationRefused("POLICY_DENIED", {"reason": "self_approval"})
    return {
        "value": {
            "repository": params["repository"],
            "candidate_set_digest": params["candidate_set_digest"],
            "source_blob_digest": params["source_blob_digest"],
            "invariant_generation": params["invariant_generation"],
            "reviewer_principal": reviewer,
        }
    }


#: The approval is a returned receipt; the engine stores it under the caller's key.
NATIVE_IDEMPOTENCY: dict[str, str] = {"policy.review": "medulla:DecisionLog"}


def operations() -> tuple[OpSpec, ...]:
    return (
        _common.build_write_op(
            op_id="policy.review",
            summary="Review a policy revision and return a bound approval",
            examples=("approve this policy revision",),
            params=PolicyReviewParams,
            result=PolicyReviewResult,
            handler="arbora.api.ops.policy.handle_policy_review",
            scope="approvals:decide",
        ),
        _common.build_read_op(
            op_id="policy.bundle.validate",
            summary="Validate a policy bundle file against the engine without binding it",
            examples=("validate this policy bundle",),
            params=BundleParams,
            result=BundleResult,
            handler="arbora.api.ops.policy.handle_bundle_validate",
            scope="policy:read",
        ),
        _common.build_write_op(
            op_id="policy.bundle.bind",
            summary="Bind a policy bundle file to its scope in the engine",
            examples=("bind this policy bundle",),
            params=BundleParams,
            result=BundleResult,
            handler="arbora.api.ops.policy.handle_bundle_bind",
            scope="fleet:control",
            effect=Effect.ADMIN,
            confirm=Confirm.CONSOLE,
        ),
        _common.build_tenant_read_op(
            op_id="policy.read",
            summary="Show this tenant's active policy state",
            examples=("show the active policy state",),
            params=PolicyReadParams,
            result=PolicyReadResult,
            handler="arbora.api.ops.policy.handle_policy_read",
            scope="policy:read",
        ),
    )


specs = operations

__all__ = [
    "PolicyReadParams",
    "PolicyReadResult",
    "PolicyReader",
    "PolicyState",
    "handle_bundle_bind",
    "handle_bundle_validate",
    "handle_policy_read",
    "operations",
    "specs",
]
