"""The held-output seam between the harness and the critique gate (ARBORA-A2A-R022).

The harness (``MERISTEM-HARNESS-CYCLE-R101``) emits the block the gate reads: with
``HarnessConfig.held_output`` set, the terminal ``graph_complete`` event carries
``result.held_output = {item_ref, content_digest, engine_decision_ref, checklist,
verification?}``. Arbora

* sets the config on the run plan from refs it is given (never invented here),
* passes ``encode`` so the harness digests exactly the bytes the gate digests (the
  canonical JSON of the task's artifacts), and
* lifts only ``{item_ref, content_digest, engine_decision_ref, checklist}`` into the
  task's ``metadata.heldOutput``; ``verification`` stays outside it.

PIN-LAG: a pinned meristem without ``HeldOutputConfig`` or ``HarnessConfig.held_output``
gets no config, so the run carries no block and the gate holds the output
(``POLICY_EVIDENCE_MISSING``); nothing is faked.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arbora.api.domain.a2a.models import A2AArtifact, A2ATextPart

__all__ = [
    "HELD_OUTPUT_FIELDS",
    "HeldOutputRefs",
    "artifacts_bytes",
    "held_metadata",
    "held_output_config",
    "lift_held_output",
    "plan_for_owner",
    "reply_artifact",
    "reply_bytes",
    "with_held_output",
]

#: The only keys the task's ``HeldOutput`` accepts (it forbids extras).
HELD_OUTPUT_FIELDS = ("item_ref", "content_digest", "engine_decision_ref", "checklist")


@dataclass(frozen=True, slots=True)
class HeldOutputRefs:
    """The refs the host received for one run's held output; the harness derives the rest."""

    item_ref: str
    engine_decision_ref: str
    #: ``(id, question)`` pairs of the critique checklist.
    checklist: tuple[tuple[str, str], ...]
    verifier: str | None = None
    compiler: str | None = None
    verification_ref: str | None = None


def artifacts_bytes(artifacts: Sequence[A2AArtifact] | None) -> bytes:
    """The exact bytes of an output: its artifacts, canonically encoded."""
    body = [
        artifact.model_dump(mode="json", by_alias=True) for artifact in artifacts or ()
    ]
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def reply_artifact(task_id: str, text: str) -> A2AArtifact:
    """The one artifact a task's reply text becomes."""
    return A2AArtifact(
        artifact_id=f"{task_id}:reply", name="reply", parts=[A2ATextPart(text=text)]
    )


def reply_bytes(task_id: str, text: str) -> bytes:
    """The bytes the gate digests for ``text`` as task ``task_id``'s reply."""
    return artifacts_bytes([reply_artifact(task_id, text)])


def held_output_config(refs: HeldOutputRefs | None, task_id: str) -> Any | None:
    """The harness ``HeldOutputConfig`` for one task, or ``None`` (no refs or pin-lag)."""
    if refs is None:
        return None
    try:
        from meristem.graph.state import HeldOutputConfig
    except ImportError:  # PIN-LAG: the pinned harness has no held output
        return None
    return HeldOutputConfig(
        item_ref=refs.item_ref,
        engine_decision_ref=refs.engine_decision_ref,
        checklist=tuple((i, q) for i, q in refs.checklist),
        verifier=refs.verifier,
        compiler=refs.compiler,
        verification_ref=refs.verification_ref,
        encode=lambda text: reply_bytes(task_id, text),
    )


def with_held_output(plan: Any, config: Any | None) -> Any:
    """``plan`` with ``harness.held_output`` set; unchanged without a config or the field."""
    harness = getattr(plan, "harness", None)
    if config is None or harness is None or not dataclasses.is_dataclass(harness):
        return plan
    if "held_output" not in {f.name for f in dataclasses.fields(harness)}:
        return plan  # PIN-LAG
    return dataclasses.replace(
        plan, harness=dataclasses.replace(harness, held_output=config)
    )


def plan_for_owner(plan: Any, owner: Any) -> Any:
    """``plan`` with the held output of the native run ``owner``, when it has refs."""
    refs = getattr(owner, "held_output", None)
    config = held_output_config(refs, getattr(owner, "task_id", ""))
    return with_held_output(plan, config)


def held_metadata(held: Mapping[str, Any] | None) -> dict[str, Any]:
    """The task metadata entry of a lifted block: ``{heldOutput: block}`` or nothing."""
    return {} if held is None else {"heldOutput": dict(held)}


def lift_held_output(result: Any) -> dict[str, Any] | None:
    """The four-key block of a ``graph_complete`` result, or ``None`` when absent."""
    block = result.get("held_output") if isinstance(result, Mapping) else None
    if not isinstance(block, Mapping) or any(
        k not in block for k in HELD_OUTPUT_FIELDS
    ):
        return None
    return {key: block[key] for key in HELD_OUTPUT_FIELDS}
