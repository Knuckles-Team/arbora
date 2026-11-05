"""Pinned items skip LRU eviction and survive the idle expiry rebuild.

ARBORA-FLEET-R059.2: the pinned set never loses an item to eviction, idle
expiry or selector retraction; an explicit unload or one-shot consume takes
the pin with it.
"""

from __future__ import annotations

import pytest

from arbora.fleet.session_loads import LoadCapExceeded, SessionLoads


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.2")
async def test_pinned_skip_lru_and_idle_expiry() -> None:
    now = [1000.0]
    loads = SessionLoads(
        cap=64, pinned_cap=32, idle_ttl_seconds=3600, clock=lambda: now[0]
    )
    ids = [f"item:{i}" for i in range(64)]
    for item_id in ids:
        now[0] += 1.0
        loads.load("s", [item_id])
    assert loads.loaded("s") == set(ids)

    pinned = ids[:3]  # the three oldest loads
    loads.pin("s", pinned)
    for extra in ("e0", "e1", "e2", "e3", "e4"):
        now[0] += 1.0
        result = loads.load("s", [extra], evict="lru")
        assert len(result["evicted"]) == 1
        assert result["evicted"][0] not in pinned

    loaded = loads.loaded("s")
    assert len(loaded) == 64
    assert set(pinned) <= loaded
    assert loaded == (set(ids) - {"item:3", "item:4", "item:5", "item:6", "item:7"}) | {
        "e0",
        "e1",
        "e2",
        "e3",
        "e4",
    }
    # selector retraction never reaches a pinned item
    assert loads.retractable("s") == loaded - set(pinned)

    # the pinned cap is enforced per session, separate from the session cap
    small = SessionLoads(cap=64, pinned_cap=2, clock=lambda: now[0])
    small.load("s", ["x0"])
    small.pin("s", ["a", "b"])
    with pytest.raises(LoadCapExceeded) as cap_exc:
        small.pin("s", ["c"])
    assert cap_exc.value.cap == "pinned"
    assert cap_exc.value.loaded
    # unpin is a no-op on an unpinned id; a pinned id is dropped
    assert small.unpin("s", ["c"]) == []
    assert small.unpin("s", ["a"]) == ["a"]
    assert small.pinned_items("s") == {"b"}

    # past the idle TTL the session rebuilds: only the pins stay loaded,
    # re-marked with their old bindings
    now[0] += 3600.0 + 1.0
    assert loads.loaded("s") == set(pinned)
    assert loads.pinned_items("s") == set(pinned)
    for item_id in pinned:
        assert loads.binding("s", item_id) == (item_id,)
    status = loads.status("s")
    assert status["cap"] == 64
    assert status["pinned_cap"] == 32
    assert status["pinned_used"] == 3
    assert status["pinned"] == sorted(pinned)
    assert status["used"] == 3
