"""Doubles for the agent-run delegation and reclaim tests (ARBORA-A2A-R001.2.2).

``install_agent_run_models`` gives the pinned release the engine ``main`` shape
(structurally: the guard reads ``model_fields``). ``ReclaimWorkItems`` is the
engine WorkItem namespace with lease expiry, a named reclaim that checks the
run's delegation, and ``CancelWorkItem``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from tests.test_agents_run_end_to_end import FakeWorkItems


class _Field:
    pass


class _DelegationRecordDouble:
    model_fields = {
        name: _Field()
        for name in (
            "expires_at",
            "kind",
            "operations",
            "principal",
            "revoked_at",
            "run_id",
            "tenant",
            "tool",
        )
    }


class _OldDelegationRecordDouble:
    model_fields = {
        name: _Field()
        for name in ("expires_at", "principal", "revoked_at", "tenant", "tool")
    }


class _ClaimResultDouble:
    model_fields = {"claimed": _Field(), "delegation_id": _Field()}


def install_agent_run_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine ``main`` shape of the agent-run delegation."""
    from medulla.generated import models

    monkeypatch.setattr(models, "DelegationKind", object, raising=False)
    monkeypatch.setattr(
        models, "DelegationRecord", _DelegationRecordDouble, raising=False
    )
    monkeypatch.setattr(
        models, "ClaimWorkItemResult", _ClaimResultDouble, raising=False
    )


def install_pinned_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned release: a delegation record without the agent-run fields."""
    from medulla.generated import models

    monkeypatch.delattr(models, "DelegationKind", raising=False)
    monkeypatch.setattr(
        models, "DelegationRecord", _OldDelegationRecordDouble, raising=False
    )


class DelegationStore:
    """The engine's delegation records, written by the ``Identity`` delegation op."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []

    async def send(self, op: Any) -> Any:
        self.sent.append(dict(op))
        request = op["request"]
        self.records[request["work_item_id"]] = {
            **request["record"],
            "revoked_at": None,
        }
        return {"ok": True}

    def revoke(self, delegation_id: str) -> None:
        self.records[delegation_id]["revoked_at"] = 1

    async def read(self, delegation_id: str) -> dict[str, Any] | None:
        """``GetDelegationRecord``: the record with its standing, or ``None``."""
        record = self.records.get(delegation_id)
        if record is None:
            return None
        standing = "revoked" if record["revoked_at"] is not None else "active"
        return {
            "record": dict(record),
            "standing": standing,
            "principal_active": True,
            "holds_tool_scope": True,
        }

    def reassign(self, delegation_id: str, principal: str) -> None:
        self.records[delegation_id]["principal"] = principal

    def active(self, delegation_id: str, now_ms: int) -> bool:
        record = self.records.get(delegation_id)
        return (
            record is not None
            and record["revoked_at"] is None
            and record["expires_at"] > now_ms
        )


class ReclaimWorkItems(FakeWorkItems):
    """Leases that expire, a delegation-checked named claim, and cancel."""

    def __init__(self, delegations: DelegationStore) -> None:
        super().__init__()
        self.delegations = delegations
        self.cancels: list[dict[str, Any]] = []

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        row = self.rows[request["work_item_id"]]
        now_ms = request["now_ms"]
        expired = row["status"] == "leased" and row["lease_expires_at_ms"] <= now_ms
        if row["status"] != "ready" and not expired:
            return {"claimed": False}
        delegation_id = row["metadata"].get("delegation_id")
        if delegation_id is not None and not self.delegations.active(
            delegation_id, now_ms
        ):
            raise OperationRefused("DELEGATION_INVALID", {})
        row["status"] = "leased"
        row["lease_expires_at_ms"] = now_ms + request["lease_ms"]
        self.leases.append(request["work_item_id"])
        token = self.leases.count(request["work_item_id"])
        row["lease"] = (request["worker_ref"], token, token)
        return {
            "claimed": True,
            "lease_holder_ref": request["worker_ref"],
            "lease_epoch": token,
            "fencing_token": token,
            "delegation_id": delegation_id,
        }

    def cancel(self, **request: Any) -> dict[str, Any]:
        self.cancels.append(request)
        self.rows[request["work_item_id"]]["status"] = "cancelled"
        return {"status": "cancelled"}
