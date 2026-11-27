"""The production ports of the CSV import (ARBORA-DATA-MARKET-R009.5 and R009.6).

``finance_import_source`` reads the uploaded file through the existing upload path: the
artifact reference is the engine blob the ``bundle.upload`` chunks staged, read back one
chunk at a time by ``bundle.download``. ``finance_activity_sink`` sends the parsed
activities to the engine ``SourceIngest`` through the SDK ingest facade, one entity per
activity whose id is the SDK's file digest plus row identity, so a second send of the same
file is the same records. Arbora holds neither the bytes nor the activities.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused

__all__ = [
    "ACTIVITY_NODE_TYPE",
    "ACTIVITY_STREAM",
    "CONNECTOR",
    "MAX_IMPORT_BYTES",
    "BlobImportSource",
    "EngineActivitySink",
    "activity_record_id",
]

#: The most one CSV import reads (16 MiB); a larger file is ``INVALID_ARGUMENT``.
MAX_IMPORT_BYTES = 16 * 1024 * 1024
CONNECTOR = "finance-import"
ACTIVITY_STREAM = "account-activity"
ACTIVITY_NODE_TYPE = "AccountActivity"


class BlobImportSource:
    """``finance_import_source``: the uploaded blob, read through ``bundle.download``."""

    async def read(self, context: Any, artifact: str) -> bytes:
        from arbora.api.ops.bundle import CHUNK_BYTES, handle_bundle_download

        body = bytearray()
        index = 0
        chunks = 1
        while index < chunks:
            if index * CHUNK_BYTES > MAX_IMPORT_BYTES:
                raise OperationRefused("INVALID_ARGUMENT", {"field": "artifact"})
            part = await handle_bundle_download(
                context, {"bundle_ref": artifact, "index": index}, None
            )
            chunks = int(part["chunks"])
            try:
                body += base64.b64decode(part["data_b64"], validate=True)
            except (binascii.Error, ValueError):
                raise OperationRefused(
                    "UNAVAILABLE", {"reason": "chunk malformed"}
                ) from None
            index += 1
        if len(body) > MAX_IMPORT_BYTES:
            raise OperationRefused("INVALID_ARGUMENT", {"field": "artifact"})
        return bytes(body)


def activity_record_id(activity: Any) -> str:
    """The stable identity of one activity: tenant, source, account and file row."""
    identity = json.dumps(
        [
            activity.tenant_id,
            activity.source_id,
            activity.provider,
            activity.account_id,
            activity.source_event_id,
        ],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode()).hexdigest()


class EngineActivitySink:
    """``finance_activity_sink``: the engine ``SourceIngest`` through the SDK facade."""

    async def ingest(self, context: Any, activities: Sequence[Any]) -> int:
        from scion_sdk.ingest import ChangeSet, Entity, IngestBinding, KnowledgeIngest
        from scion_sdk.ingest.transport import MedullaIngestTransport

        entities = tuple(
            Entity(
                id=activity_record_id(activity),
                node_type=ACTIVITY_NODE_TYPE,
                properties=activity.model_dump(mode="json"),
                updated_at=activity.observed_at_utc.isoformat(),
            )
            for activity in activities
        )
        receipt = await KnowledgeIngest(MedullaIngestTransport(context.client)).submit(
            IngestBinding(connector=CONNECTOR, stream=ACTIVITY_STREAM),
            ChangeSet(entities=entities),
        )
        return int(receipt.affected_count)
