"""Pin rollback and session restore: a store failure pins nothing; restore is quiet.

ARBORA-FLEET-R059.6 (a store failure pins nothing, including records this
request already wrote) and ARBORA-FLEET-R059.4 (every session start restores
stored pins, once per session, and a refused mount suspends the pin).
"""

from __future__ import annotations

import pytest

from arbora.fleet.pin_store import InMemoryPinStore, PinRecord
from tests.fleet._pin_harness import harness, pin


class _FailsOnSecondPut(InMemoryPinStore):
    def __init__(self) -> None:
        super().__init__()
        self.puts = 0

    async def put(self, record: PinRecord) -> None:
        self.puts += 1
        if self.puts == 2:
            raise RuntimeError("engine unreachable")
        await super().put(record)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.6")
async def test_store_failure_midway_leaves_no_partial_pin() -> None:
    state = await harness(pin_store=_FailsOnSecondPut())
    outcome = await pin(state, state.alice, ("fleet:tool:s/read", "fleet:tool:s/secret"))
    assert outcome.code == "UNAVAILABLE"
    # the first record this request wrote is dropped, and the session is unchanged
    assert await state.store.list("tenant", "alice") == ()
    assert state.ops.sessions.loaded("tenant:alice") == frozenset()
    assert state.ops.sessions.pinned_items("tenant:alice") == frozenset()


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.4")
async def test_restore_notifies_once_and_suspends_a_refused_mount() -> None:
    state = await harness()
    ops = state.ops
    assert (await pin(state, state.alice, ("fleet:tool:s/read",))).code == "OK"
    # a restart: a fresh session store, the durable record remains
    ops.sessions.__init__(pinned_cap=32)
    first = await ops.restore_pins(state.alice, "tenant:alice")
    assert first["restored"] == ["fleet:tool:s/read"]
    # the next request finds it already restored and sends no list_changed
    second = await ops.restore_pins(state.alice, "tenant:alice")
    assert second == {"restored": [], "suspended": []}

    # a mount the surface refuses suspends the pin instead of failing the request
    async def refused(_item, _call):
        raise PermissionError("native mounts need the governed surface")

    ops.sessions.__init__(pinned_cap=32)
    ops._mount = refused
    third = await ops.restore_pins(state.alice, "tenant:alice")
    assert third["restored"] == []
    assert third["suspended"] == [
        {"item_id": "fleet:tool:s/read", "code": "UNAVAILABLE"}
    ]
    assert len(await state.store.list("tenant", "alice")) == 1
