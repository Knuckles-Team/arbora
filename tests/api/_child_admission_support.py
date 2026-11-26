"""A WorkItem authority over an engine double that models admission identity."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.run_origin import RunOriginRegistry


class EngineRefusal(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdmissionEngine:
    """Engine rules for child admissions: identity, owner, revision and lifecycle."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, dict[str, Any]]] = {}
        self.delivered = 0
        self.lifecycle = {"child:1": ("active", 3), "done:1": ("completed", 5)}
        self.owners = {"child:1": "owner", "done:1": "owner"}

    def submit(self, work_engine: Any, **fields: Any) -> tuple[str, bool]:
        body = fields["metadata"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        key = fields["idempotency_key"]
        if key in self.rows:
            if self.rows[key][0] != digest:
                raise EngineRefusal("IDEMPOTENCY_CONFLICT")
            return fields.get("work_item_id") or "work:" + key, False
        target = body["target"]
        if self.owners.get(target) != fields["created_by"]:
            raise EngineRefusal("CHILD_OWNER_MISMATCH")
        state, revision = self.lifecycle.get(target, ("missing", 0))
        admission = fields.get("child_input") or fields["conversation"]
        if admission["expected_revision"] != revision:
            raise EngineRefusal(
                "CHILD_REVISION_STALE"
                if "child_input" in fields
                else "HISTORY_REVISION_CONFLICT"
            )
        if "child_input" in fields and state != "active":
            raise EngineRefusal("CHILD_TARGET_TERMINAL")
        if "conversation" in fields and state != "completed":
            raise EngineRefusal("CONTINUATION_LIFECYCLE_INVALID")
        self.rows[key] = (digest, fields)
        self.delivered += 1
        return fields.get("work_item_id") or "work:" + key, True


def authority(
    monkeypatch: pytest.MonkeyPatch, engine: AdmissionEngine, owner: str = "owner"
) -> WorkItemA2AAuthority:
    from arbora import work_items

    value = WorkItemA2AAuthority(lambda: object(), origins=RunOriginRegistry())
    monkeypatch.setattr(
        WorkItemA2AAuthority,
        "_session",
        staticmethod(lambda scope: SimpleNamespace(tenant="t1", actor="a")),
    )
    monkeypatch.setattr(value, "_engine", lambda: (object(), object()))
    monkeypatch.setattr(value, "_owner_ref", lambda session: owner)
    monkeypatch.setattr(
        work_items,
        "submit_work_item_atomic",
        lambda work_engine, **fields: engine.submit(work_engine, **fields),
    )
    return value
