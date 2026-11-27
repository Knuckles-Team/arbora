"""The approval broker and run evidence of process runs (ARBORA-OPS-R898.4, R898.7).

The broker is the meristem ``ApprovalWaiter`` port: a wait creates one pending record
keyed by run and step, and ``resolve`` decides it exactly once. Every record lives in the
engine key-value journal under the verified tenant: arbora keeps no durable store and no
process-local state, so a second broker (or a restart) reads the same pending wait. The
clock is injected, never read here.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

#: ``send(method, params, idempotency_key)`` to the decoded engine answer.
EngineSend = Callable[[str, dict[str, Any], str | None], Awaitable[Any]]

NAMESPACE = "process.journal"
VERDICTS = ("approve", "reject")
_RUN = "_run"
_DONE = "_done"


class ApprovalNotPending(Exception):
    """The step has no pending wait: absent, foreign, or already decided."""


class ApproverRoleRequired(Exception):
    """The deciding caller does not hold the step's approver role."""


def _encode(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"))


def _entry(answer: Any) -> tuple[int, dict[str, Any] | None]:
    """``(version, record)`` of one ``KvGet`` answer; version 0 means absent."""
    if not isinstance(answer, Mapping) or "value" not in answer:
        return 0, None
    raw = answer["value"]
    record = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
    return int(answer.get("version", 0)), dict(record)


class ApprovalBroker:
    """Pending and decided approvals, and the evidence of one process run."""

    def __init__(self, send: EngineSend) -> None:
        self._send = send

    @staticmethod
    def _key(tenant: str, run_id: str, step_id: str) -> str:
        return f"{tenant}/{run_id}/{step_id}"

    async def _read(self, key: str) -> tuple[int, dict[str, Any] | None]:
        answer = await self._send("KvGet", {"namespace": NAMESPACE, "key": key}, None)
        return _entry(answer)

    async def _write(
        self, key: str, version: int, record: Mapping[str, Any], idem: str
    ) -> None:
        await self._send(
            "KvCas",
            {
                "namespace": NAMESPACE,
                "key": key,
                "expected_version": version,
                "new": _encode(record),
            },
            idem,
        )

    async def wait(
        self,
        tenant: str,
        *,
        run_id: str,
        step_id: str,
        approver_role: str,
        requested_ms: int,
    ) -> dict[str, Any]:
        """Create the one pending record of a step; a repeated wait finds it unchanged."""
        key = self._key(tenant, run_id, step_id)
        version, found = await self._read(key)
        if found is not None:
            return found
        record = {
            "kind": "approval",
            "tenant": tenant,
            "run_id": run_id,
            "step_id": step_id,
            "approver_role": approver_role,
            "state": "pending",
            "requested_ms": requested_ms,
        }
        await self._write(key, version, record, f"approval-wait:{key}")
        return record

    async def read(
        self, tenant: str, run_id: str, step_id: str
    ) -> dict[str, Any] | None:
        """The one record of a step (pending or decided), or ``None``."""
        _version, record = await self._read(self._key(tenant, run_id, step_id))
        return record

    async def settle_run(
        self,
        tenant: str,
        *,
        run_id: str,
        status: str,
        branch_verdicts: Sequence[str] = (),
        decision_records: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Record how a run ended and the verdict and decision records it cited, once."""
        key = self._key(tenant, run_id, _DONE)
        version, found = await self._read(key)
        if found is not None:
            return found
        entry = {
            "kind": "settled",
            "tenant": tenant,
            "run_id": run_id,
            "status": status,
            "branch_verdicts": list(branch_verdicts),
            "decision_records": list(decision_records),
        }
        await self._write(key, version, entry, f"run-settled:{key}")
        return entry

    async def resolve(
        self,
        tenant: str,
        *,
        run_id: str,
        step_id: str,
        verdict: str,
        comment: str,
        principal: str,
        groups: Sequence[str],
        decided_ms: int,
    ) -> dict[str, Any]:
        """Decide one pending wait once, as ``principal``; anything else is not pending."""
        if verdict not in VERDICTS:
            raise ValueError(f"verdict is one of {VERDICTS}")
        key = self._key(tenant, run_id, step_id)
        version, record = await self._read(key)
        if record is None or record.get("state") != "pending":
            raise ApprovalNotPending
        if record["approver_role"] not in groups:
            raise ApproverRoleRequired
        decided = {
            **record,
            "state": "decided",
            "verdict": verdict,
            "comment": comment,
            "approver": principal,
            "decided_ms": decided_ms,
        }
        try:
            await self._write(key, version, decided, f"approval-decide:{key}")
        except Exception as conflict:  # a concurrent decision won the CAS
            if getattr(conflict, "code", "") not in {
                "CAS_CONFLICT",
                "VERSION_MISMATCH",
            }:
                raise
            raise ApprovalNotPending from None
        return decided

    async def records(self, tenant: str, run_id: str) -> list[dict[str, Any]]:
        """Every approval of one run (pending and decided), then none of another tenant."""
        found: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            request: dict[str, Any] = {
                "namespace": NAMESPACE,
                "prefix": f"{tenant}/{run_id}/",
                "limit": 200,
            }
            if cursor:
                request["cursor"] = cursor
            page = await self._send("KvScan", request, None)
            rows = page.get("entries", []) if isinstance(page, Mapping) else []
            for row in rows:
                _version, record = _entry(row)
                if record and record.get("tenant") == tenant:
                    found.append(record)
            cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
            if not cursor:
                return found

    async def journal_run(
        self,
        tenant: str,
        *,
        run_id: str,
        plan_digest: str,
        workflow_id: str,
        version: int,
        binding_sources: Mapping[str, str],
        branch_verdicts: Sequence[str] = (),
        decision_records: Sequence[str] = (),
    ) -> dict[str, Any]:
        """Record the plan digest, workflow identity and the record ids a run cites."""
        key = self._key(tenant, run_id, _RUN)
        stored, found = await self._read(key)
        if found is not None:
            return found
        entry = {
            "kind": "run",
            "tenant": tenant,
            "run_id": run_id,
            "plan_digest": plan_digest,
            "workflow_id": workflow_id,
            "workflow_version": version,
            "binding_sources": dict(sorted(binding_sources.items())),
            "branch_verdicts": list(branch_verdicts),
            "decision_records": list(decision_records),
        }
        await self._write(key, stored, entry, f"run-journal:{key}")
        return entry

    async def evidence(self, tenant: str, run_id: str) -> dict[str, Any]:
        """The run entry and each approval with approver, time and verdict."""
        rows = await self.records(tenant, run_id)
        by_kind: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_kind.setdefault(str(row.get("kind")), []).append(row)
        return {
            "run": (by_kind.get("run") or [None])[0],
            "settled": (by_kind.get("settled") or [None])[0],
            "approvals": by_kind.get("approval", []),
        }


__all__ = [
    "NAMESPACE",
    "VERDICTS",
    "ApprovalBroker",
    "ApprovalNotPending",
    "ApproverRoleRequired",
    "EngineSend",
]
