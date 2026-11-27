"""A WorkItem authority over an engine double that models admission identity."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.core.settings import RuntimeConfig
from arbora.run_origin import RunOriginRegistry

DONE = "a2a-" + "b" * 64
LIVE = "a2a-" + "a" * 64


class EngineRefusal(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class AdmissionEngine:
    """Engine rules for child admissions: identity, owner, revision and lifecycle."""

    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, dict[str, Any]]] = {}
        self.delivered = 0
        self.lifecycle = {
            "child:1": ("active", 3),
            DONE: ("completed", 5),
            LIVE: ("active", 3),
        }
        #: A target absent here belongs to whoever calls; set it to model a foreign one.
        self.owners: dict[str, str] = {}
        #: The agent each predecessor ran on, read back by a continuation.
        self.agent = "helper"
        self.runs: list[dict[str, Any]] = []

    def item(self, work_item_id: str, caller: str) -> dict[str, Any] | None:
        """The predecessor WorkItem a continuation reads: its owner and chosen agent."""
        target = work_item_id.removeprefix("workitem:orchestrator:")
        if target not in self.lifecycle:
            return None
        return {
            "work_item_id": work_item_id,
            "kind": "orchestrator_task",
            "status": "succeeded",
            "metadata": {
                "a2a_schema": "arbora-a2a-unary-v1",
                "a2a_owner_ref": self.owners.get(target, caller),
                "a2a_context_id": "ctx-" + target[-4:],
                "a2a_route": {
                    "agentName": self.agent,
                    "selectionMode": "explicit",
                },
            },
        }

    def submit(self, work_engine: Any, **fields: Any) -> tuple[str, bool]:
        body = fields["metadata"]
        digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        key = fields["idempotency_key"]
        if key in self.rows:
            if self.rows[key][0] != digest:
                raise EngineRefusal("IDEMPOTENCY_CONFLICT")
            return fields.get("work_item_id") or "work:" + key, False
        target = body["target"]
        if self.owners.get(target, fields["created_by"]) != fields["created_by"]:
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


def install_engine(
    monkeypatch: pytest.MonkeyPatch, engine: AdmissionEngine, owner: str = "owner"
) -> None:
    """Route the authority's WorkItem engine and run start to the double."""
    from arbora import work_items

    monkeypatch.setattr(
        WorkItemA2AAuthority, "_owner_ref", staticmethod(lambda session: owner)
    )
    monkeypatch.setattr(
        WorkItemA2AAuthority, "_engine", lambda self: (object(), object())
    )
    monkeypatch.setattr(
        work_items,
        "submit_work_item_atomic",
        lambda work_engine, **fields: engine.submit(work_engine, **fields),
    )
    monkeypatch.setattr(
        work_items,
        "get_work_item",
        lambda work_engine, item_id: engine.item(item_id, owner),
    )

    async def run(_self: Any, _work_engine: Any, **fields: Any) -> None:
        engine.runs.append(fields)

    monkeypatch.setattr(WorkItemA2AAuthority, "_run", run)


def authority(
    monkeypatch: pytest.MonkeyPatch, engine: AdmissionEngine, owner: str = "owner"
) -> WorkItemA2AAuthority:
    install_engine(monkeypatch, engine, owner)
    monkeypatch.setattr(
        WorkItemA2AAuthority,
        "_session",
        staticmethod(lambda scope: SimpleNamespace(tenant="t1", actor="a")),
    )
    return WorkItemA2AAuthority(
        lambda: object(), origins=RunOriginRegistry(), config=RuntimeConfig()
    )
