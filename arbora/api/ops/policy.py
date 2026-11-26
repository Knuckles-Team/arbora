"""Policy read and lifecycle operations (ARBORA-OPS-R021.3, ARBORA-OPS-R050).

``policy.create``, ``policy.update`` and ``policy.retire`` send the engine
``DecisionLog`` op ``policy_activation`` (MEDULLA-ORCH-VOCAB-R806) with the
repository, the change kind and the review receipt digest; the engine checks the
receipt, runs its one activation guard and answers the active scope and one
entity reference per clause. Arbora reads no clause text, holds no policy copy
and publishes nothing locally. ``policy.source.bind`` sends the engine
``PolicySourceBind`` request for a reviewed source file.

``policy.read`` hands back the caller's own tenant's active policy state
from the composed ``policy_reader`` port. The OpSpec wiring, the
not-composed-yet refusal, and the trivial params/result pair all come from
:mod:`arbora.api.ops._common`; this module only supplies the port
protocol, the state shape, and the service name.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Iterator, Mapping
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _common
from arbora.api.ops._engine import engine_send
from arbora.api.registry import Confirm, Effect, OpSpec
from arbora.control_plane.runs.engine_policy import ENGINE_POLICY_DENIALS
from arbora.deployment.policy_bundle import BundleRefused, load_bundle
from arbora.pin_lag.medulla import (
    PinLagUnavailable,
    refusal,
    require_methods,
    require_models,
)

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


_DIGEST = re.compile(r"[0-9a-f]{64}")
WRITE_SCOPE = "agent:decision-write"


class PolicyChange(StrEnum):
    """The closed set of policy lifecycle changes of the engine op."""

    CREATE = "create"
    UPDATE = "update"
    RETIRE = "retire"


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _digest(value: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError("a digest is 64 lowercase hex characters")
    return value


class ConstitutionSource(_Frozen):
    """The reviewed repository file a policy change reads its clauses from."""

    repository: str = Field(min_length=1, max_length=256)
    path: str = Field(min_length=1, max_length=512)
    commit: str = Field(min_length=7, max_length=64)
    blob_digest: str

    _blob = field_validator("blob_digest")(_digest)


class PolicyEntity(_Frozen):
    """One clause entity the engine read into the active policy."""

    policy_ref: str = Field(min_length=1)
    version_digest: str
    constitution_ref: str = Field(min_length=1)
    clause_digest: str
    state: Literal["active", "retired"]

    _version = field_validator("version_digest")(_digest)
    _clause = field_validator("clause_digest")(_digest)


class PolicyChangeParams(_common.Params):
    constitution: ConstitutionSource
    receipt_digest: str

    _receipt = field_validator("receipt_digest")(_digest)


class PolicyChangeResult(_common.Params):
    policy_ref: str
    change: PolicyChange
    revision: str | None = None
    version_digest: str
    entities: list[PolicyEntity]


class PolicySourceBindParams(_common.Params):
    source_ref: str = Field(min_length=1, max_length=512)
    source_revision: str = Field(min_length=1, max_length=128)
    source_digest: str
    policy_iri: str = Field(min_length=1, max_length=512)
    approver: str = Field(min_length=1, max_length=256)
    component_version: str = Field(min_length=1, max_length=128)

    _source = field_validator("source_digest")(_digest)


class PolicySourceBindResult(_common.Params):
    binding: dict[str, Any]


def _entities(
    answer: Mapping[str, Any], *, retired: bool, constitution: str
) -> list[PolicyEntity]:
    state = "retired" if retired else "active"
    digest = str(answer.get("candidate_set_digest", ""))
    return [
        PolicyEntity(
            policy_ref=str(answer["scope_ref"]),
            version_digest=digest,
            constitution_ref=constitution,
            clause_digest=str(row["clause_digest"]),
            state=state,
        )
        for row in answer.get("entities", [])
    ]


@contextlib.contextmanager
def policy_guard() -> Iterator[None]:
    """Read the engine's policy refusals as the row's codes (ARBORA-OPS-R050.4).

    An approval or scope the engine refuses is ``POLICY_DENIED`` and names the
    policy reference the engine returned; an unreachable engine is
    ``POLICY_UNAVAILABLE``. Nothing else is rewritten, and the guarded call
    never runs again after a refusal.
    """
    try:
        yield
    except EngineRefusal as refused:
        if refused.code not in ENGINE_POLICY_DENIALS:
            raise
        details = {"engine_code": refused.code}
        if refused.details.get("policy_ref"):
            details["policy_ref"] = str(refused.details["policy_ref"])
        raise OperationRefused("POLICY_DENIED", details) from refused
    except (OSError, TimeoutError) as unreachable:
        raise OperationRefused("POLICY_UNAVAILABLE") from unreachable


async def _policy_engine(context: Any, method: str, params: dict[str, Any]) -> Any:
    with policy_guard():
        return await engine_send(context, method, params, idempotent=True)


async def handle_policy_change(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Activate, replace or retire one policy through the engine decision write."""
    try:
        require_models(("DecisionLogOpPolicyActivation",))
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    change = PolicyChange(op.id.rsplit(".", 1)[1])
    constitution = ConstitutionSource.model_validate(params["constitution"])
    answer = await _policy_engine(
        context,
        "DecisionLog",
        {
            "op": {
                "op": "policy_activation",
                "tenant_id": str(context.caller.tenant),
                "change": change.value,
                "source": constitution.repository,
                "receipt_digest": params["receipt_digest"],
            }
        },
    )
    if not isinstance(answer, Mapping) or "scope_ref" not in answer:
        raise OperationRefused("UNAVAILABLE", {"reason": "engine result malformed"})
    entities = _entities(
        answer,
        retired=change is PolicyChange.RETIRE,
        constitution=f"{constitution.repository}@{constitution.commit}:{constitution.path}",
    )
    return PolicyChangeResult(
        policy_ref=str(answer["scope_ref"]),
        change=change,
        revision=answer.get("revision"),
        version_digest=str(answer.get("candidate_set_digest", "")),
        entities=entities,
    ).model_dump(mode="json")


async def handle_policy_source_bind(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Bind a reviewed source file to the active policy; engine refusals pass through."""
    try:
        require_methods("PolicySourceBind")
    except PinLagUnavailable as missing:
        raise refusal(missing) from missing
    answer = await engine_send(
        context, "PolicySourceBind", {"request": dict(params)}, idempotent=True
    )
    return {"binding": dict(answer) if isinstance(answer, Mapping) else {}}


#: The approval is a returned receipt; the engine stores it under the caller's key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "policy.create": "medulla:DecisionLog",
    "policy.update": "medulla:DecisionLog",
    "policy.retire": "medulla:DecisionLog",
    "policy.source.bind": "medulla:PolicySourceBind",
}


def _change_op(op_id: str, summary: str) -> OpSpec:
    return _common.build_write_op(
        op_id=op_id,
        summary=summary,
        examples=(summary.lower(),),
        params=PolicyChangeParams,
        result=PolicyChangeResult,
        handler="arbora.api.ops.policy.handle_policy_change",
        scope=WRITE_SCOPE,
        confirm=Confirm.PLAN,
    )


def operations() -> tuple[OpSpec, ...]:
    return (
        _change_op(
            "policy.create", "Activate a policy from a reviewed repository file"
        ),
        _change_op("policy.update", "Replace the active policy from a reviewed file"),
        _change_op("policy.retire", "Retire the active policy of a repository"),
        _common.build_write_op(
            op_id="policy.source.bind",
            summary="Bind a reviewed policy source file to the active policy",
            examples=("bind this reviewed policy source",),
            params=PolicySourceBindParams,
            result=PolicySourceBindResult,
            handler="arbora.api.ops.policy.handle_policy_source_bind",
            scope=WRITE_SCOPE,
            confirm=Confirm.PLAN,
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
    "NATIVE_IDEMPOTENCY",
    "ConstitutionSource",
    "PolicyChange",
    "PolicyChangeParams",
    "PolicyChangeResult",
    "PolicyEntity",
    "PolicySourceBindParams",
    "handle_policy_change",
    "handle_policy_source_bind",
    "policy_guard",
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
