"""D18 write-back: a typed, idempotent, previewed registry operation.

ARBORA-FLEET-R003: the typed preview model and its refusal rule (.1), and
the registered preview/commit operations (.2) whose gate and receipts
Medulla holds through the effect journal.  D18 write-back never executes
without a matching prior preview and an idempotency key.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import OpError, OpResult
from arbora.api.registry import (
    AuditClass,
    Composite,
    Confirm,
    Effect,
    Idempotency,
    OpSpec,
    Verb,
)

#: The registry binding this preview is written for once wired into the
#: registry (ARBORA-FLEET-R003.2): always previewed, always keyed.
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


class WriteBackCommitResult(BaseModel):
    """The receipt of one committed write-back; replays return it verbatim."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    connector: str
    target: str
    idempotency_key: str
    receipt: dict[str, Any]
    applied: bool = True


#: The commit effect owner: given the verified caller and (connector, target,
#: change) it performs the governed connector write and returns its receipt.
#: The served bootstrap binds it over the governed fleet gateway.
WriteBackEffect = Callable[
    [Any, str, str, dict[str, Any]], Awaitable[Mapping[str, Any]]
]


def _binding(request: WriteBackPreviewRequest) -> str:
    """The canonical previewed change a commit must match exactly."""
    return json.dumps(
        {
            "connector": request.connector,
            "target": request.target,
            "change": request.change,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


class WriteBackLedger:
    """Preview gate and commit replay held in Medulla through the effect journal.

    ARBORA-FLEET-R003.2: a preview is one journaled effect whose binding is
    the canonical change; a commit first re-reserves that preview key with
    its own change, so a missing or different preview is refused.  The commit
    is a second journaled effect: Medulla reserves it at most once, and a
    replay returns the stored receipt without calling the connector again.
    Keys are scoped to the caller's tenant and principal.  Arbora keeps no
    local copy, so the gate and replay survive a restart.
    """

    def __init__(self, journal: Any) -> None:
        self._journal = journal

    @staticmethod
    def _key(scope: str, phase: str, key: str) -> str:
        return f"write-back:{scope}:{phase}:{key}"

    async def record_preview(
        self, scope: str, request: WriteBackPreviewRequest
    ) -> WriteBackPreviewResult:
        result = preview_write_back(request)
        held = await self._journal.reserve(
            self._key(scope, "preview", request.idempotency_key), _binding(request)
        )
        if held.state == "acquired":
            await self._journal.complete(
                held.reference, OpResult(result.model_dump(mode="json"))
            )
            return result
        if held.state == "completed" and isinstance(held.outcome, OpResult):
            return WriteBackPreviewResult(**held.outcome.value)
        if held.state == "conflict":
            raise WriteBackRefused("idempotency key was previewed for another change")
        raise WriteBackRefused("idempotency key is not available for a preview")

    async def commit(
        self,
        scope: str,
        caller: Any,
        request: WriteBackPreviewRequest,
        effect: WriteBackEffect,
    ) -> WriteBackCommitResult:
        key = request.idempotency_key
        binding = _binding(request)
        preview = await self._journal.reserve(self._key(scope, "preview", key), binding)
        if preview.state == "acquired":
            # No preview existed: close the reservation so the key stays spent.
            await self._journal.complete(
                preview.reference, OpError("WRITE_BACK_NO_PREVIEW")
            )
            raise WriteBackRefused("write-back commit has no matching preview")
        if preview.state == "conflict":
            raise WriteBackRefused(
                "write-back commit does not match its previewed change"
            )
        if preview.state != "completed" or not isinstance(preview.outcome, OpResult):
            raise WriteBackRefused("write-back commit has no matching preview")
        held = await self._journal.reserve(self._key(scope, "commit", key), binding)
        if held.state == "completed" and isinstance(held.outcome, OpResult):
            return WriteBackCommitResult(**held.outcome.value)
        if held.state != "acquired":
            raise WriteBackRefused("write-back commit is pending or was refused")
        try:
            receipt = await effect(
                caller, request.connector, request.target, dict(request.change)
            )
        except Exception as exc:
            await self._journal.complete(
                held.reference,
                OpError("WRITE_BACK_FAILED", {"error": type(exc).__name__}, "fleet"),
            )
            raise
        result = WriteBackCommitResult(
            connector=request.connector,
            target=request.target,
            idempotency_key=key,
            receipt=dict(receipt),
        )
        await self._journal.complete(
            held.reference, OpResult(result.model_dump(mode="json"))
        )
        return result


def _bound(context: Any, name: str) -> Any:
    value = context.services.get(name)
    if value is None:
        raise OperationRefused("UNAVAILABLE", {"reason": f"{name} is not bound"})
    return value


def _scope(context: Any) -> str:
    return f"{context.caller.tenant}:{context.caller.principal}"


async def handle_write_back_preview(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = WriteBackPreviewRequest(**dict(params))
    result = await _bound(context, "write_back_ledger").record_preview(
        _scope(context), request
    )
    return {"value": result.model_dump()}


async def handle_write_back_commit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = WriteBackPreviewRequest(**dict(params))
    result = await _bound(context, "write_back_ledger").commit(
        _scope(context),
        context.caller,
        request,
        _bound(context, "write_back_effect"),
    )
    return {"value": result.model_dump()}


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="fleet.write_back.preview",
            verb=Verb.WRITE,
            summary="Preview a connector write-back without applying it",
            examples=("preview this connector write-back",),
            params=WriteBackPreviewRequest,
            result=WriteBackPreviewResult,
            binding=Composite(
                handler="arbora.api.ops.write_back.handle_write_back_preview"
            ),
            scopes=frozenset({"mcp:delegate"}),
            effect=Effect.READ,
            idempotency=Idempotency.KEY_REQUIRED,
        ),
        OpSpec(
            id="fleet.write_back.commit",
            verb=Verb.WRITE,
            summary="Commit a previously previewed connector write-back",
            examples=("commit this previewed connector write-back",),
            params=WriteBackPreviewRequest,
            result=WriteBackCommitResult,
            binding=Composite(
                handler="arbora.api.ops.write_back.handle_write_back_commit"
            ),
            scopes=frozenset({"mcp:delegate"}),
            effect=Effect.WRITE,
            confirm=WRITE_BACK_CONFIRM,
            idempotency=WRITE_BACK_IDEMPOTENCY,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations

__all__ = [
    "WriteBackCommitResult",
    "WriteBackEffect",
    "WriteBackLedger",
    "handle_write_back_commit",
    "handle_write_back_preview",
    "operations",
    "specs",
    "WRITE_BACK_CONFIRM",
    "WRITE_BACK_IDEMPOTENCY",
    "WriteBackPreviewRequest",
    "WriteBackPreviewResult",
    "WriteBackRefused",
    "preview_write_back",
]
