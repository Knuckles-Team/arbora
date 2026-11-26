"""Tool-task Agent Card extension and approval attestation (ARBORA-A2A-R002.2.4).

The attestation names an approval record that lives in the graph and binds it to
one task. It carries no grant, scope or credential: the engine validates the
record when the task executes, and Arbora never reads the attestation back as
authority.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from typing import Any, Protocol, runtime_checkable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field

from .models import A2AExtension, A2AMessage, A2ATask
from .routing import A2AAssemblyUnavailable

EXTENSION_URI = "urn:arbora:a2a:ext:tool-task:v1"
DATA_KEY = "toolTask"
ATTESTATION_DOMAIN = "arbora.a2a.tool-task-attestation"
_DIGEST = r"^[0-9a-f]{64}$"

#: Message and task metadata key Arbora alone writes.
ARBORA_METADATA = "arbora"


class ToolTaskInput(BaseModel):
    """The one caller-supplied tool call; it carries no approval of any kind."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str = Field(min_length=1, max_length=256)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolTaskAttestation(BaseModel):
    """Arbora's signature over (task id, approval record id, digest)."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    approval_record_id: str = Field(min_length=1, max_length=512)
    digest: str = Field(pattern=_DIGEST)
    signature: str = Field(min_length=1, max_length=256)
    key_id: str = Field(min_length=1, max_length=128)

    def as_metadata(self) -> dict[str, str]:
        return {
            "approvalRecordId": self.approval_record_id,
            "digest": self.digest,
            "signature": self.signature,
            "keyId": self.key_id,
        }


@runtime_checkable
class AttestationSigner(Protocol):
    key_id: str

    def sign(self, data: bytes) -> bytes: ...


@runtime_checkable
class ToolTaskAdmitter(Protocol):
    """Admit one tool call as the verified caller; return the task and its record."""

    async def admit(
        self, *, call: ToolTaskInput, message: A2AMessage, idempotency_key: str
    ) -> tuple[A2ATask, str, str]:
        """The task, the approval record id and the approved params digest."""
        ...


def attestation_bytes(task_id: str, approval_record_id: str, digest: str) -> bytes:
    return json.dumps(
        [ATTESTATION_DOMAIN, task_id, approval_record_id, digest],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def sign_attestation(
    signer: AttestationSigner, *, task_id: str, approval_record_id: str, digest: str
) -> ToolTaskAttestation:
    raw = signer.sign(attestation_bytes(task_id, approval_record_id, digest))
    return ToolTaskAttestation(
        approval_record_id=approval_record_id,
        digest=digest,
        signature=base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        key_id=signer.key_id,
    )


def verify_attestation(
    public_key: Ed25519PublicKey, attestation: ToolTaskAttestation, *, task_id: str
) -> bool:
    padded = attestation.signature + "=" * (-len(attestation.signature) % 4)
    try:
        public_key.verify(
            base64.urlsafe_b64decode(padded),
            attestation_bytes(
                task_id, attestation.approval_record_id, attestation.digest
            ),
        )
    except (InvalidSignature, binascii.Error, ValueError):
        return False
    return True


def tool_task_extension() -> A2AExtension:
    return A2AExtension(
        uri=EXTENSION_URI,
        description=(
            "Run one registry operation as a task. The request names the operation "
            "and parameters only; the task result carries an approval attestation."
        ),
        required=False,
        params={"dataKey": DATA_KEY, "metadataKey": ARBORA_METADATA},
    )


def reject_client_authority(message: A2AMessage) -> None:
    """Refuse any approval the caller tried to supply."""
    if ARBORA_METADATA in message.metadata:
        raise ValueError("tool_task_client_approval_refused")


def tool_task_calls(message: A2AMessage) -> list[ToolTaskInput]:
    """The tool calls in the message data parts, refusing client approvals."""
    calls: list[ToolTaskInput] = []
    for data in message.data_parts():
        body = data.get(DATA_KEY)
        if body is None:
            continue
        if not isinstance(body, Mapping) or set(body) - {"operation", "parameters"}:
            raise ValueError("tool_task_client_approval_refused")
        calls.append(ToolTaskInput.model_validate(dict(body)))
    if len(calls) > 1:
        raise ValueError("tool_task_one_call_per_message")
    return calls


async def admit_tool_task(
    admitter: ToolTaskAdmitter | None,
    signer: AttestationSigner | None,
    message: A2AMessage,
    call: ToolTaskInput,
    idempotency_key: str,
) -> A2ATask:
    if admitter is None or signer is None:
        raise A2AAssemblyUnavailable("tool_task_unavailable")
    task, record_id, digest = await admitter.admit(
        call=call, message=message, idempotency_key=idempotency_key
    )
    if not record_id:
        raise A2AAssemblyUnavailable("tool_task_approval_record_missing")
    attestation = sign_attestation(
        signer, task_id=task.id, approval_record_id=record_id, digest=digest
    )
    metadata = {**task.metadata, ARBORA_METADATA: attestation.as_metadata()}
    return task.model_copy(update={"metadata": metadata})


__all__ = [
    "ARBORA_METADATA",
    "DATA_KEY",
    "EXTENSION_URI",
    "AttestationSigner",
    "ToolTaskAdmitter",
    "ToolTaskAttestation",
    "ToolTaskInput",
    "admit_tool_task",
    "attestation_bytes",
    "reject_client_authority",
    "sign_attestation",
    "tool_task_calls",
    "tool_task_extension",
    "verify_attestation",
]
