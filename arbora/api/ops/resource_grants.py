"""The per-tenant resource-grant ledger for incremental indexing (ARBORA-OPS-R073).

The ledger is engine-held: one engine KV entry per tenant, changed only by
compare-and-set, so a restart or a second host never forgets a held grant and
no process-local state decides admission. The engine keeps allocation
accounting; this ledger only bounds the working bytes held at once per tenant.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.engine_calls import engine_send

#: The engine KV namespace, one key per tenant (the caller's tenant keys the entry).
NAMESPACE = "resource_grants"
#: Compare-and-set attempts before a contended ledger refuses.
MAX_ATTEMPTS = 8


def _held(entry: Any) -> tuple[dict[str, int], int]:
    """The held grants (job key to bytes) and the entry's KV version."""
    if not isinstance(entry, Mapping) or "value" not in entry:
        return {}, 0
    raw = entry["value"]
    if isinstance(raw, (list, tuple)):
        raw = bytes(raw)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        raw = json.loads(bytes(raw))
    held = raw.get("held", {}) if isinstance(raw, Mapping) else {}
    return {str(k): int(v) for k, v in held.items()}, int(entry.get("version", 0))


class ResourceGrantLedger:
    """Admit and release working-byte grants for one caller's tenant."""

    def __init__(self, context: Any, *, limit: int) -> None:
        self._context = context
        self._limit = limit
        self._key = str(context.caller.tenant)

    async def _read(self) -> tuple[dict[str, int], int]:
        entry = await engine_send(
            self._context, "KvGet", {"namespace": NAMESPACE, "key": self._key}
        )
        return _held(entry)

    async def _swap(self, held: dict[str, int], version: int) -> bool:
        answer = await engine_send(
            self._context,
            "KvCas",
            {
                "namespace": NAMESPACE,
                "key": self._key,
                "expected_version": version,
                "new": json.dumps({"held": held}, sort_keys=True).encode(),
            },
            idempotent=True,
        )
        return bool(isinstance(answer, Mapping) and answer.get("swapped"))

    async def admit(self, job_key: str, max_working_bytes: int) -> None:
        """Record the job's positive grant, or refuse ``LOAD_CAP_EXCEEDED``."""
        if max_working_bytes < 1:
            raise OperationRefused("INVALID_ARGUMENT", {"field": "max_working_bytes"})
        for _ in range(MAX_ATTEMPTS):
            held, version = await self._read()
            others = sum(v for k, v in held.items() if k != job_key)
            if others + max_working_bytes > self._limit:
                raise OperationRefused(
                    "LOAD_CAP_EXCEEDED",
                    {"limit": self._limit, "requested": max_working_bytes},
                )
            if await self._swap({**held, job_key: max_working_bytes}, version):
                return
        raise OperationRefused("REVISION_CONFLICT", {"ledger": NAMESPACE})

    async def release(self, job_key: str) -> None:
        """Drop the job's grant; a grant already gone is not an error."""
        for _ in range(MAX_ATTEMPTS):
            held, version = await self._read()
            if job_key not in held:
                return
            remaining = {k: v for k, v in held.items() if k != job_key}
            if await self._swap(remaining, version):
                return
        raise OperationRefused("REVISION_CONFLICT", {"ledger": NAMESPACE})
