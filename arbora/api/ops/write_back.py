"""D18 write-back: a typed, idempotent, previewed registry operation.

ARBORA-FLEET-R003: the typed preview model and its refusal rule (.1), and the
registered repository operations ``fleet.write_back.preview`` (read) and
``fleet.write_back.commit`` (write, plan-confirmed, key-required) (.2, .5 to
.10). D18 write-back never executes without a confirmed plan and an
idempotency key, so it is never reachable as an unguarded native child tool.

The commit resolves one admitted binding from trusted configuration, sends the
SDK request to the bound connector tool through the governed fleet gateway, and
returns a result only when the SDK receipt binds to the request. An unresolved
or unbound path refuses before any provider mutation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.write_back_models import (
    RepositoryWriteBackParams,
    RepositoryWriteBackResult,
)
from arbora.api.registry import (
    AuditClass,
    Confirm,
    Effect,
    Idempotency,
    OpSpec,
    PrincipalRule,
    Verb,
)

#: The largest request the parser reads before the trusted binding supplies the
#: tighter per-source payload limit.
HARD_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024
BINDINGS_FILE = "write_back_bindings.yaml"

#: The registry binding this preview is written for once wired into the
#: registry (ARBORA-FLEET-R003 follow-up): always previewed, always keyed.
WRITE_BACK_CONFIRM = Confirm.PLAN
WRITE_BACK_IDEMPOTENCY = Idempotency.KEY_REQUIRED


class WriteBackPreviewRequest(BaseModel):
    """A connector write-back candidate, always evaluated as a preview."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str = Field(min_length=1)
    target: str = Field(min_length=1)
    change: dict[str, Any]
    idempotency_key: str = Field(min_length=1)


class WriteBackPreviewResult(BaseModel):
    """What a D18 write-back preview returns: a plan, never an applied write."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str
    target: str
    idempotency_key: str
    would_apply: dict[str, Any]
    applied: bool = False


class WriteBackRefused(ValueError):
    """Raised when a write-back is attempted outside the preview protocol."""


def preview_write_back(request: WriteBackPreviewRequest) -> WriteBackPreviewResult:
    """Return the typed, idempotent preview for a D18 write-back candidate.

    This is the only entry point this module exposes. There is no apply/execute
    counterpart here: D18 write-back is exposed solely as a previewed registry
    operation (``Confirm.PLAN``, ``Idempotency.KEY_REQUIRED`` at the registry
    layer), never as a native child tool that writes directly.
    """

    if not request.idempotency_key.strip():
        raise WriteBackRefused(
            "write-back preview requires a non-empty idempotency key"
        )
    return WriteBackPreviewResult(
        connector=request.connector,
        target=request.target,
        idempotency_key=request.idempotency_key,
        would_apply=dict(request.change),
        applied=False,
    )


@dataclass(frozen=True)
class WriteBackBinding:
    """One admitted repository source: connector, source instance and tool."""

    connector_id: str
    source_instance_id: str
    repository_id: str
    server: str
    tool: str
    max_payload_size: int

    def digest(self) -> str:
        canonical = json.dumps(
            {
                "connector_id": self.connector_id,
                "source_instance_id": self.source_instance_id,
                "repository_id": self.repository_id,
                "server": self.server,
                "tool": self.tool,
                "max_payload_size": self.max_payload_size,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WriteBackBindings:
    """The trusted bindings, read from configuration, never from a request."""

    def __init__(self, entries: tuple[WriteBackBinding, ...] = ()) -> None:
        self._entries = entries

    def find(
        self, connector_id: str, source_instance_id: str, repository_id: str
    ) -> WriteBackBinding | None:
        for entry in self._entries:
            if (entry.connector_id, entry.source_instance_id, entry.repository_id) == (
                connector_id,
                source_instance_id,
                repository_id,
            ):
                return entry
        return None


def load_write_back_bindings(path: Path | None = None) -> WriteBackBindings:
    """Read the admitted bindings from ``~/.config/arbora``; absent means none."""
    import yaml

    from arbora.core.settings import config_dir

    target = path or config_dir() / BINDINGS_FILE
    if not target.is_file():
        return WriteBackBindings()
    document = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    rows = document.get("bindings", []) if isinstance(document, dict) else []
    return WriteBackBindings(
        tuple(
            WriteBackBinding(
                connector_id=str(row["connector_id"]),
                source_instance_id=str(row["source_instance_id"]),
                repository_id=str(row["repository_id"]),
                server=str(row["server"]),
                tool=str(row["tool"]),
                max_payload_size=int(row["max_payload_size"]),
            )
            for row in rows
        )
    )


def _refuse(code: str, **details: Any) -> Exception:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused(f"WRITE_BACK_{code}", details)


def _invalid(field: str) -> Exception:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused("INVALID_ARGUMENT", {"field": field})


def parse_repository_request(request: Mapping[str, Any]) -> Any:
    """Parse one SDK ``RepositorySourceEffectRequest``; refuse a malformed one.

    The SDK model forbids unknown fields and checks the intent against the
    canonical change set.
    """
    from scion_sdk.writeback import RepositorySourceEffectRequest

    try:
        return RepositorySourceEffectRequest.from_wire_json(
            json.dumps(request).encode("utf-8"),
            max_payload_size=HARD_MAX_PAYLOAD_BYTES,
        )
    except ValidationError as exc:
        errors = exc.errors()
        if any(item["type"] == "extra_forbidden" for item in errors):
            raise _refuse("UNKNOWN_FIELD") from None
        if any("canonical change set" in str(item.get("msg", "")) for item in errors):
            raise _refuse("INTENT_MISMATCH") from None
        raise _invalid("request") from None
    except ValueError:
        raise _invalid("request") from None


def resolve_binding(
    context: Any, request: Any, params: RepositoryWriteBackParams
) -> WriteBackBinding:
    """Resolve the one admitted binding of a request, from trusted config only."""
    bindings = context.services.get("write_back_bindings")
    if bindings is None:
        raise _refuse("PRODUCER_MISSING", reason="no write-back binding is composed")
    change = request.change_set
    binding = bindings.find(
        change.connector_id, change.source_instance_id, request.intent.repository_id
    )
    if binding is None:
        raise _refuse("PRODUCER_MISSING", reason="no admitted binding for this source")
    if binding.digest() != params.binding_digest:
        raise _refuse("BINDING_CHANGED")
    try:
        request.intent.require_payload_limit(binding.max_payload_size)
    except ValueError:
        raise _invalid("payload") from None
    return binding


def _preview_projection(request: Any, binding: WriteBackBinding) -> dict[str, Any]:
    intent = request.intent
    change = request.change_set
    return {
        "applied": False,
        "connector_id": change.connector_id,
        "source_instance_id": change.source_instance_id,
        "repository_id": intent.repository_id,
        "target_ref": intent.target_ref,
        "relative_path": intent.relative_path,
        "action": intent.action,
        "base_revision": intent.base_revision,
        "payload_sha256": intent.payload_sha256,
        "payload_size": intent.payload_size,
        "executable": intent.executable,
        "change_set_digest": change.change_set_digest,
        "effect_key": change.idempotency_key,
        "binding_digest": binding.digest(),
    }


async def handle_write_back_preview(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Project the exact effect a commit would apply; nothing is written."""
    typed = RepositoryWriteBackParams.model_validate(dict(params))
    request = parse_repository_request(typed.request)
    binding = resolve_binding(context, request, typed)
    return {"value": _preview_projection(request, binding)}


def _verified_receipt(request: Any, payload: Any) -> Any:
    from scion_sdk.ports.audit_reservation import AuditReservation
    from scion_sdk.writeback import RepositorySourceEffectReceipt

    value = payload.get("value", payload) if isinstance(payload, Mapping) else None
    raw = value.get("receipt") if isinstance(value, Mapping) else None
    try:
        receipt = RepositorySourceEffectReceipt.model_validate(raw)
        change = request.change_set
        receipt.require_binding(
            request,
            AuditReservation(
                reservation_id=receipt.audit_reservation_id,
                effect_key=change.idempotency_key,
                change_set_digest=change.change_set_digest,
            ),
        )
    except (ValidationError, ValueError):
        raise _refuse("RECEIPT_UNBOUND") from None
    return receipt


async def handle_write_back_commit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Apply one confirmed repository effect through the governed fleet gateway."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.fleet.service_child import ServiceChildOutcomeUnknown

    key = getattr(context, "idempotency_key", None)
    if not isinstance(key, str) or not key.startswith("plan:"):
        raise _refuse("PLAN_UNBOUND")
    typed = RepositoryWriteBackParams.model_validate(dict(params))
    request = parse_repository_request(typed.request)
    binding = resolve_binding(context, request, typed)
    current = context.services["write_back_bindings"].find(
        binding.connector_id, binding.source_instance_id, binding.repository_id
    )
    if current is None or current.digest() != binding.digest():
        raise _refuse("CONFIG_CHANGED")
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise _refuse("PRODUCER_MISSING", reason="fleet gateway is not bound")
    try:
        result = await gateway.call(
            context.caller,
            binding.server,
            binding.tool,
            {"request": typed.request},
            expected_effect=Effect.WRITE,
            service_identity=context.service_identity,
            owner=context.owner,
            owner_ref=getattr(context, "owner_ref", None),
            fleet_decision=context.fleet_decision,
            registry_digest=context.registry_digest,
            approval_ref=key.removeprefix("plan:"),
        )
    except ServiceChildOutcomeUnknown:
        raise _refuse("EFFECT_UNCERTAIN") from None
    except OperationRefused:
        raise
    except Exception:  # an unreadable outcome is never a success
        raise _refuse("REPLAY_UNRESOLVED") from None
    receipt = _verified_receipt(request, result)
    return {
        "value": {
            "applied": True,
            "receipt": receipt.model_dump(mode="json"),
            "request_digest": request.change_set.change_set_digest,
            "receipt_digest": receipt.receipt_digest(),
            "effect_revision": receipt.effect_revision,
        }
    }


class WriteBackProposalApproveParams(Params):
    queue_id: str = Field(min_length=1, max_length=256)
    proposal_id: str = Field(min_length=1, max_length=256)


class WriteBackProposalsListParams(Params):
    queue_id: str = Field(min_length=1, max_length=256)


class WriteBackValueResult(Params):
    value: dict[str, Any]


APPROVE_SCOPE = "approvals:decide"


def _writeback_published() -> bool:
    """Whether the pinned contract declares the ``DecisionLog`` write-back operation."""
    from medulla.generated import models

    return hasattr(models, "DecisionLogOpWriteback")


async def _writeback(context: Any, computation: dict[str, Any]) -> dict[str, Any]:
    answer = await engine_send(
        context,
        "DecisionLog",
        {
            "op": {
                "op": "writeback",
                "tenant_id": str(context.caller.tenant),
                "computation": computation,
            }
        },
        idempotent=computation["kind"] == "approve",
    )
    return dict(answer) if isinstance(answer, Mapping) else {"items": answer}


async def handle_write_back_proposals_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read the queued proposals of one queue; the engine owns the queue."""
    return {
        "value": await _writeback(
            context, {"kind": "list", "queue_id": params["queue_id"]}
        )
    }


async def handle_write_back_proposal_approve(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Record one approval with the approver principal; release stays with the engine."""
    from arbora.api.invoke.pipeline import OperationRefused

    if APPROVE_SCOPE not in (getattr(context.caller, "effective_scopes", None) or ()):
        raise OperationRefused("SCOPE_REQUIRED", {"missing_scopes": [APPROVE_SCOPE]})
    return {
        "value": await _writeback(
            context,
            {
                "kind": "approve",
                "queue_id": params["queue_id"],
                "proposal_id": params["proposal_id"],
            },
        )
    }


#: The engine decision log replays each approval (ARBORA-OPS-R865).
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "write_back.proposal.approve": "medulla:DecisionLog",
}


def _proposal_operations() -> tuple[OpSpec, ...]:
    if not _writeback_published():
        return ()
    return (
        build_read_op(
            op_id="write_back.proposals.list",
            verb=Verb.FIND,
            summary="List the queued write-back proposals",
            examples=("list queued write-back proposals",),
            params=WriteBackProposalsListParams,
            result=WriteBackValueResult,
            handler="arbora.api.ops.write_back.handle_write_back_proposals_list",
            scope="kg:read",
        ),
        build_write_op(
            op_id="write_back.proposal.approve",
            verb=Verb.ACT,
            summary="Approve one queued write-back proposal",
            examples=("approve this write-back proposal",),
            params=WriteBackProposalApproveParams,
            result=WriteBackValueResult,
            handler="arbora.api.ops.write_back.handle_write_back_proposal_approve",
            scope=APPROVE_SCOPE,
            confirm=Confirm.PLAN,
            principals=PrincipalRule.HUMAN,
        ).model_copy(update={"audit": AuditClass.IDENTITY_CHAIN}),
    )


def operations() -> tuple[OpSpec, ...]:
    """Register write-back as typed, gated operations, never a native child tool."""
    return (
        *_proposal_operations(),
        build_read_op(
            op_id="fleet.write_back.preview",
            verb=Verb.ASK,
            summary="Preview one repository write-back without applying it",
            examples=("preview this repository change",),
            params=RepositoryWriteBackParams,
            result=RepositoryWriteBackResult,
            handler="arbora.api.ops.write_back.handle_write_back_preview",
            scope="kg:read",
        ),
        build_write_op(
            op_id="fleet.write_back.commit",
            verb=Verb.ACT,
            summary="Commit one previewed and approved repository write-back",
            examples=("commit this approved repository change",),
            params=RepositoryWriteBackParams,
            result=RepositoryWriteBackResult,
            handler="arbora.api.ops.write_back.handle_write_back_commit",
            scope="kg:write",
            confirm=Confirm.PLAN,
        ),
    )


specs = operations

__all__ = [
    "WRITE_BACK_CONFIRM",
    "WRITE_BACK_IDEMPOTENCY",
    "WriteBackBinding",
    "WriteBackBindings",
    "WriteBackPreviewRequest",
    "WriteBackPreviewResult",
    "WriteBackRefused",
    "handle_write_back_commit",
    "handle_write_back_preview",
    "load_write_back_bindings",
    "operations",
    "parse_repository_request",
    "preview_write_back",
    "specs",
]
