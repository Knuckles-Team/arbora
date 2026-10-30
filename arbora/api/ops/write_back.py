"""D18 write-back: a typed, idempotent, previewed registry operation.

ARBORA-FLEET-R003 (.1 slice): the typed preview model and its refusal rule —
D18 write-back must never execute without a prior preview and an idempotency
key, so it can never be reached as an unguarded native child tool. Wiring a
live handler/binding into the operation registry is a further slice.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

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


#: The commit effect owner: given (connector, target, change) it performs the
#: write and returns its receipt. Bound as service ``write_back_effect``.
WriteBackEffect = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class WriteBackLedger:
    """Replay ledger: previews and committed receipts keyed by idempotency key."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._previews: dict[str, WriteBackPreviewResult] = {}
        self._receipts: dict[str, WriteBackCommitResult] = {}

    def record_preview(self, result: WriteBackPreviewResult) -> None:
        with self._lock:
            self._previews[result.idempotency_key] = result

    def commit(
        self, request: WriteBackPreviewRequest, effect: WriteBackEffect
    ) -> WriteBackCommitResult:
        key = request.idempotency_key
        with self._lock:
            preview = self._previews.get(key)
            if preview is None:
                raise WriteBackRefused("write-back commit has no matching preview")
            if (
                preview.connector != request.connector
                or preview.target != request.target
                or preview.would_apply != request.change
            ):
                raise WriteBackRefused(
                    "write-back commit does not match its previewed change"
                )
            prior = self._receipts.get(key)
            if prior is not None:
                return prior
            receipt = effect(request.connector, request.target, dict(request.change))
            result = WriteBackCommitResult(
                connector=request.connector,
                target=request.target,
                idempotency_key=key,
                receipt=receipt,
            )
            self._receipts[key] = result
            return result


def _bound(context: Any, name: str) -> Any:
    value = context.services.get(name)
    if value is None:
        raise RuntimeError(f"write-back {name} is not bound")
    return value


async def handle_write_back_preview(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = WriteBackPreviewRequest(**dict(params))
    result = preview_write_back(request)
    _bound(context, "write_back_ledger").record_preview(result)
    return {"value": result.model_dump()}


async def handle_write_back_commit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = WriteBackPreviewRequest(**dict(params))
    result = _bound(context, "write_back_ledger").commit(
        request, _bound(context, "write_back_effect")
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
