"""Critique-gated output release for ``message/send`` (ARBORA-A2A-R022).

A generated output enters the held state: the run's answer carries a typed critique
checklist and the engine's held-item reference (``metadata.heldOutput``). The reply is
released only when the engine's held-item admission, bound to the digest of that exact
output, answers ``admitted``. The gate reuses the one :class:`HeldItemAdmitter` of the
approval chain (``ARBORA-A2A-R006.4``) and adds no approval authority; routing in
``OrchestratorA2ARouter.route`` is never consulted. Any missing, untyped, mismatched,
rejected or abstained evidence leaves the output held and sends nothing.
"""

from __future__ import annotations

import hashlib
from typing import Any

from pydantic import ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from arbora.api.domain.a2a.held_item_admission import HeldItemRefusal, HeldItemSubject
from arbora.api.domain.a2a.models import A2ATask, WireModel
from arbora.api.domain.a2a.tool_action_admission import SHA256_HEX

from .confirm import ApprovalChain
from .held_output import artifacts_bytes

__all__ = [
    "HELD_OUTPUT_KEY",
    "ChecklistItem",
    "HeldOutput",
    "output_digest",
    "release_output",
]

#: The task metadata key that carries the held output reference.
HELD_OUTPUT_KEY = "heldOutput"


class ChecklistItem(WireModel):
    """One typed question the critique answered about the output."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    id: str = Field(min_length=1, max_length=128)
    question: str = Field(min_length=1, max_length=512)


class HeldOutput(WireModel):
    """The typed critique preflight of one generated output."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )

    item_ref: str = Field(min_length=1, max_length=512)
    content_digest: str = Field(pattern=SHA256_HEX)
    engine_decision_ref: str = Field(min_length=1, max_length=512)
    checklist: tuple[ChecklistItem, ...] = Field(min_length=1, max_length=64)


def output_digest(task: A2ATask) -> str:
    """The digest of the exact output: the task's artifacts, canonically encoded."""
    return hashlib.sha256(artifacts_bytes(task.artifacts)).hexdigest()


def _preflight(task: A2ATask) -> HeldOutput:
    raw: Any = task.metadata.get(HELD_OUTPUT_KEY)
    if not isinstance(raw, dict):
        raise HeldItemRefusal("POLICY_EVIDENCE_MISSING")
    try:
        held = HeldOutput.model_validate(raw)
    except ValidationError:
        raise HeldItemRefusal("POLICY_EVIDENCE_MISSING") from None
    if held.content_digest != output_digest(task):
        raise HeldItemRefusal("DIGEST_MISMATCH")
    return held


async def release_output(
    task: A2ATask, chain: ApprovalChain | None, *, tenant: str
) -> A2ATask:
    """Return ``task`` only when the engine admits its exact output.

    A task with no output has nothing to hold. Without a composed approval chain the
    host serves no critique gate and the task passes unchanged; a chain without a
    held-item admitter refuses ``ENGINE_UNAVAILABLE``.
    """
    if chain is None or not isinstance(task, A2ATask) or not task.artifacts:
        return task
    if chain.held_items is None:
        raise HeldItemRefusal("ENGINE_UNAVAILABLE")
    held = _preflight(task)
    verdict = await chain.held_items.admit(
        HeldItemSubject(
            item_kind="run_output_proposal",
            item_ref=held.item_ref,
            content_digest=held.content_digest,
            engine_decision_ref=held.engine_decision_ref,
        ),
        tenant=tenant,
    )
    return task.model_copy(
        update={
            "metadata": {
                **task.metadata,
                "critique": {
                    "outcome": verdict.outcome,
                    "verdictRef": verdict.verdict_ref,
                },
            }
        }
    )
