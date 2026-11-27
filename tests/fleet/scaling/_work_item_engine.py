"""A stateful stand-in for the engine's WorkItem namespace.

It keeps what the engine keeps for the scale ledger: one item per submit idempotency
key (a replay of a different command digest is refused), a claim with an epoch and a
fencing token, a version that every write bumps, a compare-and-set of metadata fenced
by the live lease, and a defer that releases the claim.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused


class FakeWorkItems:
    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, tuple[str, str]] = {}
        self.events: list[str] = []
        self.refuse_effect_marker = False

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        key, digest = request["idempotency_key"], request["command_digest"]
        item_id = request["work_item_id"]
        if key in self.keys:
            if self.keys[key] != (item_id, digest):
                raise OperationRefused("IDEMPOTENCY_CONFLICT", {})
            return {"work_item_id": item_id, "created": False}
        self.keys[key] = (item_id, digest)
        self.items[item_id] = {
            "work_item_id": item_id,
            "kind": request["kind"],
            "status": "ready",
            "version": 1,
            "metadata": copy.deepcopy(request["metadata"]),
            "lease": None,
            "epoch": 0,
            "token": 0,
        }
        if request["kind"] == "scale_intent":
            self.events.append("persist_intent")
        return {"work_item_id": item_id, "created": True}

    def get(self, *, tenant: str, work_item_id: str) -> dict[str, Any] | None:
        item = self.items.get(work_item_id)
        return None if item is None else copy.deepcopy(item)

    def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        item = self.items[request["work_item_id"]]
        if item["status"] != "ready":
            return {"claimed": False, "reason": "empty"}
        item.update(status="claimed", epoch=item["epoch"] + 1, token=item["token"] + 1)
        item["lease"] = request["worker_ref"]
        return {
            "claimed": True,
            "lease_epoch": item["epoch"],
            "fencing_token": item["token"],
            "lease_holder_ref": request["worker_ref"],
        }

    def _fenced(
        self, item: dict[str, Any], worker: str, epoch: int, token: int
    ) -> bool:
        return (item["lease"], item["epoch"], item["token"]) == (worker, epoch, token)

    def cas_metadata(self, **kw: Any) -> dict[str, Any]:
        item = self.items[kw["work_item_id"]]
        fence = kw.get("expected_lease")
        if fence is not None and not self._fenced(
            item, fence["worker_ref"], fence["lease_epoch"], fence["fencing_token"]
        ):
            return {"outcome": "lease_mismatch"}
        if kw["expected_metadata"] != item["metadata"]:
            return {"outcome": "metadata_mismatch"}
        item["metadata"] = copy.deepcopy(kw["set_metadata"])
        item["version"] += 1
        if "break_glass" in item["metadata"].get("scale", {}):
            self.events.append("break_glass:audit")
        return {"outcome": "applied"}

    def mark_effect_started(self, **kw: Any) -> dict[str, Any]:
        item = self.items[kw["work_item_id"]]
        live = self._fenced(
            item, kw["worker_id"], kw["lease_epoch"], kw["fencing_token"]
        )
        if (
            self.refuse_effect_marker
            or not live
            or kw["expected_revision"] != item["version"]
        ):
            raise RuntimeError("TRANSITION_REFUSED")
        item["version"] += 1
        return {"status": "effect_started"}

    def defer(self, **kw: Any) -> dict[str, Any]:
        item = self.items[kw["work_item_id"]]
        if not self._fenced(
            item, kw["worker_id"], kw["lease_epoch"], kw["fencing_token"]
        ):
            return {"status": "fenced"}
        item.update(status="ready", lease=None)
        self.events.append("defer")
        return {"status": "deferred"}

    def expire_lease(self, work_item_id: str) -> None:
        """The lease lapses: the next claim is granted."""
        self.items[work_item_id].update(status="ready", lease=None)


def engine() -> tuple[Any, FakeWorkItems]:
    work_items = FakeWorkItems()
    return SimpleNamespace(work_items=work_items), work_items


class Session:
    tenant = "t1"

    @staticmethod
    def engine_verified_context() -> dict[str, Any]:
        return {"tenant_id": "t1", "graph": "g1"}
