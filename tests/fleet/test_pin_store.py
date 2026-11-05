"""Durable pin records: the model, the fixture store, the engine carrier.

ARBORA-FLEET-R059.1 and R059.3. ``EgPinStore`` writes through the engine
control-lease protocol the served invocation already composes for plan
storage; the loopback double below speaks that protocol, so a second store
client sees the first client's pins and nobody else's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from arbora.core.settings import ArboraConfig
from arbora.fleet.pin_store import EgPinStore, InMemoryPinStore, PinRecord
from arbora.fleet.session_loads import SessionLoads


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.1")
async def test_pin_record_round_trips_and_cap_range() -> None:
    record = PinRecord(
        tenant="t",
        principal="alice",
        item_id="fleet:tool:s/read",
        binding=("fleet:tool:s/read", "tool", "s", "read"),
        pinned_at_ms=17,
    )
    store = InMemoryPinStore()
    await store.put(record)
    assert await store.list("t", "alice") == (record,)
    # the same binding is a no-op, a fresh principal sees nothing
    await store.put(record)
    assert await store.list("t", "alice") == (record,)
    assert await store.list("t", "bob") == ()
    await store.delete("t", "alice", "fleet:tool:s/read")
    assert await store.list("t", "alice") == ()

    # the record is frozen and every field round-trips
    with pytest.raises(AttributeError):
        record.item_id = "fleet:tool:s/write"
    assert (
        record.tenant,
        record.principal,
        record.item_id,
        record.binding,
        record.pinned_at_ms,
    ) == (
        "t",
        "alice",
        "fleet:tool:s/read",
        ("fleet:tool:s/read", "tool", "s", "read"),
        17,
    )

    # the knob: the FLEET_PINNED_CAP setting beside mcp_always_load_tools
    assert ArboraConfig().fleet_pinned_cap == 32
    assert ArboraConfig(FLEET_PINNED_CAP="4").fleet_pinned_cap == 4
    assert SessionLoads().pinned_cap == 32
    assert SessionLoads(pinned_cap=1).pinned_cap == 1
    assert SessionLoads(pinned_cap=128).pinned_cap == 128
    # the pinned cap is separate from the session cap
    assert SessionLoads(cap=16, pinned_cap=128).cap == 16
    with pytest.raises(ValueError):
        SessionLoads(pinned_cap=0)
    with pytest.raises(ValueError):
        SessionLoads(pinned_cap=129)


class _LeaseEngine:
    """A loopback double of the engine control-lease protocol."""

    def __init__(self) -> None:
        self.leases: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _view(lease: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "lease_id": lease["lease_id"],
            "kind": lease["kind"],
            "status": lease["status"],
            "grant": lease["grant"],
            "issued_at_ms": lease["issued_at_ms"],
            "expires_at_ms": lease["expires_at_ms"],
            "hard_expires_at_ms": lease["hard_expires_at_ms"],
            "revision": lease["revision"],
        }

    async def issue(
        self,
        tenant: str,
        lease_id: str,
        kind: str,
        grant: Mapping[str, Any],
        issued_at_ms: int,
        expires_at_ms: int,
        hard_expires_at_ms: int,
        idempotency_key: str,
    ) -> dict[str, str]:
        if lease_id in self.leases:
            return {"outcome": "collision"}
        self.leases[lease_id] = {
            "tenant": tenant,
            "lease_id": lease_id,
            "kind": kind,
            "grant": dict(grant),
            "issued_at_ms": issued_at_ms,
            "expires_at_ms": expires_at_ms,
            "hard_expires_at_ms": hard_expires_at_ms,
            "status": "active",
            "revision": 1,
        }
        return {"outcome": "issued"}

    async def get(self, tenant: str, lease_id: str) -> dict[str, Any] | None:
        lease = self.leases.get(lease_id)
        if lease is None or lease["tenant"] != tenant or lease["status"] != "active":
            return None
        return self._view(lease)

    async def list(
        self,
        tenant: str,
        kind: str,
        status: str | None,
        grant_match: Mapping[str, Any] | None,
        cursor: str | None,
        limit: int,
    ) -> dict[str, Any]:
        rows = [
            self._view(lease)
            for lease in self.leases.values()
            if lease["tenant"] == tenant
            and lease["kind"] == kind
            and (status is None or lease["status"] == status)
            and all(
                lease["grant"].get(key) == value
                for key, value in (grant_match or {}).items()
            )
        ]
        return {"leases": rows, "next_cursor": None}

    async def transition(
        self,
        tenant: str,
        lease_id: str,
        expected_revision: int,
        to: str,
        idempotency_key: str,
    ) -> dict[str, str]:
        lease = self.leases.get(lease_id)
        if lease is None or lease["tenant"] != tenant:
            return {"outcome": "not_found"}
        if lease["revision"] != expected_revision:
            return {"outcome": "conflict"}
        lease["status"] = to
        lease["revision"] += 1
        return {"outcome": "applied"}


class _LeaseClient:
    """The ``client`` shape ``EgPinStore`` reads: one ``control_leases``."""

    def __init__(self, leases: Any) -> None:
        self.control_leases = leases


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.3")
async def test_eg_pin_store_survives_a_new_client() -> None:
    engine = _LeaseEngine()
    first = EgPinStore(_LeaseClient(engine))
    record = PinRecord(
        tenant="t",
        principal="alice",
        item_id="fleet:tool:s/read",
        binding=("fleet:tool:s/read", "tool", "s", "read"),
        pinned_at_ms=7,
    )
    await first.put(record)
    # a second store client on the same loopback engine lists the pins
    second = EgPinStore(_LeaseClient(engine))
    assert await second.list("t", "alice") == (record,)
    # ...and nobody else's, in either principal or tenant
    assert await second.list("t", "bob") == ()
    assert await second.list("other", "alice") == ()
    # the same binding does not issue a second lease
    await second.put(record)
    assert len(engine.leases) == 1
    # a changed binding revokes the prior lease and issues a fresh one
    changed = PinRecord(
        tenant="t",
        principal="alice",
        item_id="fleet:tool:s/read",
        binding=("fleet:tool:s/read", "tool", "s", "read-v2"),
        pinned_at_ms=9,
    )
    await second.put(changed)
    assert await second.list("t", "alice") == (changed,)
    assert (
        sum(1 for lease in engine.leases.values() if lease["status"] == "active") == 1
    )
    # delete drops the pin for a fresh client too
    await second.delete("t", "alice", "fleet:tool:s/read")
    assert await EgPinStore(_LeaseClient(engine)).list("t", "alice") == ()
    # and a tenant-wide page never crosses tenants
    await second.put(changed)
    other = PinRecord(
        tenant="other",
        principal="alice",
        item_id="fleet:tool:s/read",
        binding=("fleet:tool:s/read", "tool", "s", "read-v2"),
        pinned_at_ms=11,
    )
    await EgPinStore(_LeaseClient(engine)).put(other)
    page, next_cursor = await second.list_tenant("t", None, 100)
    assert [record.item_id for record in page] == ["fleet:tool:s/read"]
    assert next_cursor is None
