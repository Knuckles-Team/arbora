"""Record the kept original and the transcript link in the engine (ARBORA-OPS-R899.6).

Each attachment becomes one ``MediaAttachment`` record keyed by its content
digest (``media:<sha256>``) with its type, size, filename and entry-point
source; the bytes stay at that source and are never copied into the graph.
Each derivation becomes one ``Transcript`` (or ``MediaDerivation``) record
whose ``transcribedFrom`` names the attachment record, so the transcript always
links back to its audio. Both writes are create-if-absent, so a repeated voice
note records once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from arbora.modality.models import NormalizedPrompt
from arbora.unique_records import create_record, has_record_writer

__all__ = ["engine_recorder", "provenance_records", "record_provenance"]

logger = logging.getLogger(__name__)


def provenance_records(
    prompt: NormalizedPrompt,
) -> list[tuple[str, str, dict[str, Any]]]:
    """The ``(node id, label, properties)`` records for one normalised prompt."""
    now = time.time()
    records: list[tuple[str, str, dict[str, Any]]] = []
    for attachment in prompt.attachments:
        records.append(
            (
                attachment.attachment_id,
                "MediaAttachment",
                {
                    "digest": f"sha256:{attachment.digest}",
                    "media_type": attachment.media_type,
                    "modality": attachment.modality.value,
                    "size_bytes": attachment.size_bytes,
                    "filename": attachment.filename,
                    "source": attachment.source,
                    "received_at": now,
                },
            )
        )
    for derivation in prompt.derivations:
        label = "Transcript" if derivation.kind == "transcript" else "MediaDerivation"
        records.append(
            (
                derivation.derivation_id,
                label,
                {
                    "kind": derivation.kind,
                    "text": derivation.text,
                    "transcribedFrom": derivation.derived_from,
                    "language": derivation.language or "",
                    "duration_s": derivation.duration_s,
                    "provider_server": derivation.provider_server,
                    "provider_tool": derivation.provider_tool,
                    "derived_at": now,
                },
            )
        )
    return records


def record_provenance(engine: Any, prompt: NormalizedPrompt) -> int:
    """Write the records; return how many were newly created."""
    if not has_record_writer(engine):
        return 0
    created = 0
    for node_id, label, properties in provenance_records(prompt):
        created += bool(create_record(engine, node_id, label, properties))
    return created


def engine_recorder(
    engine_for: Callable[[], Any],
) -> Callable[[NormalizedPrompt], Any]:
    """A best-effort recorder: a failed provenance write never drops the prompt."""

    async def record(prompt: NormalizedPrompt) -> None:
        try:
            engine = engine_for()
            await asyncio.to_thread(record_provenance, engine, prompt)
        except Exception as exc:  # noqa: BLE001 - provenance is logged, not fatal
            logger.warning("modality provenance write failed: %s", type(exc).__name__)

    return record
