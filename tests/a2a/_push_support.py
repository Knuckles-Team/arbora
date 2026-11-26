"""An in-memory engine ``work_items`` namespace for the push delivery tests."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from arbora.a2a.authority import (
    _A2A_METADATA_SCHEMA,
    _WORK_ITEM_PREFIX,
    WorkItemA2AAuthority,
)
from arbora.a2a.push import PushDeliveryWorker, PushSettings
from arbora.run_origin import RunOriginRegistry
from tests.a2a.test_operation_bridge import session as _session

TASK_ID = "a2a-" + "3" * 64
ITEM_ID = f"{_WORK_ITEM_PREFIX}{TASK_ID}"
START = time.time()


class Clock:
    """The injected clock: it moves only when the test moves it."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> float:
        return self.now


class PushWorkItems:
    """The engine WorkItem methods the push path calls, over a shared dict."""

    def __init__(self, clock: Clock) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.clock = clock
        self.fence = 0

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        key = request["idempotency_key"]
        if key in self.keys:
            return {"work_item_id": self.keys[key], "created": False}
        item_id = f"push-{len(self.rows)}"
        self.keys[key] = item_id
        self.rows[item_id] = {
            "work_item_id": item_id,
            "kind": request["kind"],
            "status": "ready",
            "metadata": dict(request["metadata"]),
            "max_attempts": request["max_attempts"],
            "attempt": 0,
            "not_before_ms": 0,
            "updated_at_ms": 1,
        }
        return {"work_item_id": item_id, "created": True}

    def get(self, *, tenant: str, work_item_id: str) -> dict[str, Any] | None:
        row = self.rows.get(work_item_id)
        return None if row is None else {**row, "metadata": dict(row["metadata"])}

    def list(
        self, *, tenant: str, kind: str, cursor: Any, limit: int
    ) -> dict[str, Any]:
        items = [self.get(tenant=tenant, work_item_id=i) for i, r in self.rows.items()]
        return {"items": [r for r in items if r and r["kind"] == kind]}

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        row = self.rows.get(request["work_item_id"])
        if (
            row is None
            or row["status"] != "ready"
            or row["not_before_ms"] > request["now_ms"]
        ):
            return {"claimed": False}
        self.fence += 1
        row["status"] = "leased"
        row["attempt"] += 1
        return {
            "claimed": True,
            "attempt": row["attempt"],
            "max_attempts": row["max_attempts"],
            "lease_holder_ref": request["worker_ref"],
            "lease_epoch": 1,
            "fencing_token": self.fence,
        }

    def commit_result(self, **kw: Any) -> dict[str, Any]:
        row = self.rows[kw["work_item_id"]]
        row["status"] = kw["outcome"]
        row["error_ref"] = kw.get("error_ref")
        return {"status": kw["outcome"]}

    def defer(self, **kw: Any) -> dict[str, Any]:
        row = self.rows[kw["work_item_id"]]
        row["status"] = "ready"
        row["not_before_ms"] = kw["next_retry_at_ms"]
        return {"status": "deferred"}

    def cas_metadata(self, **kw: Any) -> dict[str, Any]:
        row = self.rows[kw["work_item_id"]]
        if (
            row["status"] not in kw["expected_status"]
            or row["metadata"] != kw["expected_metadata"]
        ):
            return {"outcome": "conflict"}
        row["metadata"] = dict(kw["set_metadata"])
        return {"outcome": "applied"}


class Target:
    """A webhook receiver double: records each post, answers scripted statuses."""

    def __init__(self, *statuses: int) -> None:
        self.statuses = list(statuses)
        self.posts: list[tuple[str, dict[str, str], dict[str, Any]]] = []

    def __call__(
        self, method: str, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> int:
        self.posts.append((url, headers, body))
        return self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]


def public(host: str) -> list[str]:
    """A resolver that answers every host with one public address."""
    return ["93.184.216.34"]


def world(
    work_items: PushWorkItems | None = None,
    *,
    clock: Clock | None = None,
    post: Any = None,
    max_attempts: int = 3,
    allowed: tuple[str, ...] = (),
    resolve: Any = public,
) -> SimpleNamespace:
    """One host process: an authority and its push worker over a shared engine."""
    clock = clock or Clock()
    items = work_items or PushWorkItems(clock)
    engine = SimpleNamespace(work_items=items)
    who = _session(frozenset({"kg:read", "kg:write"}))
    authority = WorkItemA2AAuthority(
        lambda: engine, origins=RunOriginRegistry(), clock=clock
    )
    worker = PushDeliveryWorker(
        authority,
        settings=PushSettings(
            max_attempts=max_attempts,
            backoff_base_s=5,
            backoff_cap_s=20,
            allowed_private_hosts=allowed,
        ),
        clock=clock,
        post=post or Target(200),
        resolve=resolve,
        resolve_token=lambda ref: f"token-for-{ref}",
    )
    authority.push = worker
    return SimpleNamespace(
        items=items, authority=authority, worker=worker, who=who, clock=clock
    )


def seed_task(host: SimpleNamespace, *, status: str = "ready") -> None:
    """Put the caller's A2A task WorkItem on the engine (idempotent per task)."""
    from arbora.security.session import use_session

    with use_session(host.who):
        owner = host.authority._owner_ref(host.who)
    host.items.rows.setdefault(
        ITEM_ID,
        {
            "work_item_id": ITEM_ID,
            "kind": "orchestrator_task",
            "status": status,
            "metadata": {
                "a2a_schema": _A2A_METADATA_SCHEMA,
                "a2a_owner_ref": owner,
                "a2a_context_id": "a2a-context-" + "4" * 64,
            },
            "updated_at_ms": 1,
        },
    )
