"""Pin audit records and the tenant-scoped admin view.

ARBORA-FLEET-R059.8: every pin and unpin is a WRITE op with audit class
EVENT, so the invoke pipeline writes one audit record per operation with
the caller, tenant and parameter digest; ``fleet.pins.list`` pages every
pin in the caller's tenant and never crosses tenants.
"""

from __future__ import annotations

import pytest

from arbora.api.invoke import invoke
from arbora.api.registry import Surface
from tests.fleet._pin_harness import caller_for, harness, pin


def _audit_events(state) -> list[dict[str, str]]:
    return [call.args[0] for call in state.services.audit_write.await_args_list]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.8")
async def test_pin_audit_and_tenant_scoped_admin_view() -> None:
    state = await harness()
    admin = caller_for(("fleet:control", "mcp:admin"), principal="admin")

    # alice pins, bob pins his own item; carol never gets to
    assert (await pin(state, state.alice, ("fleet:tool:s/read",))).code == "OK"
    assert (await pin(state, state.bob, ("fleet:tool:s/read",))).code == "OK"
    assert (await pin(state, state.carol, ("fleet:tool:s/read",))).code == (
        "SCOPE_REQUIRED"
    )

    # every successful pin and unpin wrote an audit record
    outcome = await invoke(
        "fleet.tools.unpin",
        {"items": ["fleet:tool:s/read"]},
        state.alice,
        Surface.MCP,
        services=state.services,
        idempotency_key="unpin:one",
    )
    assert outcome.code == "OK"
    events = _audit_events(state)
    pin_events = [event for event in events if event["op"] == "fleet.tools.pin"]
    unpin_events = [event for event in events if event["op"] == "fleet.tools.unpin"]
    assert len(pin_events) == 2
    assert len(unpin_events) == 1
    for event in pin_events + unpin_events:
        assert event["tenant"] == "tenant"
        assert event["plan_digest"]
        assert event["request_id"]
    assert {event["principal"] for event in pin_events} == {"alice", "bob"}
    assert unpin_events[0]["principal"] == "alice"

    # the admin view pages every pin in the caller's tenant, with the
    # suspended state read from the owning principal's own session
    outcome = await invoke(
        "fleet.pins.list", {}, admin, Surface.MCP, services=state.services
    )
    assert outcome.code == "OK"
    view = outcome.value["value"]
    rows = {row["item_id"]: row for row in view["items"]}
    assert set(rows) == {"fleet:tool:s/read"}
    assert {row["principal"] for row in rows.values()} == {"bob"}
    # bob's pin still holds his item loaded; alice unpinned hers
    assert rows["fleet:tool:s/read"]["principal"] == "bob"
    assert rows["fleet:tool:s/read"]["suspended"] is False
    assert rows["fleet:tool:s/read"]["binding"] == [
        "fleet:tool:s/read",
        "tool",
        "s",
        "read",
    ]
    assert view["next_cursor"] is None

    # a pin not loaded in its session's principal scope shows as suspended;
    # the tenant-wide pager walks the principals one page at a time (the
    # served params carry no principal filter: the invoke pipeline rejects
    # caller-asserted identity fields)
    store = state.store
    from arbora.fleet.pin_store import PinRecord

    await store.put(
        PinRecord(
            tenant="tenant",
            principal="carol",
            item_id="fleet:tool:s/read",
            binding=("fleet:tool:s/read", "tool", "s", "read"),
            pinned_at_ms=1,
        )
    )
    outcome = await invoke(
        "fleet.pins.list",
        {"limit": 1},
        admin,
        Surface.MCP,
        services=state.services,
    )
    page = outcome.value["value"]
    assert [row["principal"] for row in page["items"]] == ["bob"]
    assert page["next_cursor"] is not None
    outcome = await invoke(
        "fleet.pins.list",
        {"limit": 1, "cursor": page["next_cursor"]},
        admin,
        Surface.MCP,
        services=state.services,
    )
    suspended_row = outcome.value["value"]["items"][0]
    assert suspended_row["principal"] == "carol"
    assert suspended_row["suspended"] is True
    assert outcome.value["value"]["next_cursor"] is None

    # an admin from another tenant never sees the pins
    other_admin = caller_for(
        ("fleet:control", "mcp:admin"),
        principal="admin",
        tenant="other",
    )
    outcome = await invoke(
        "fleet.pins.list",
        {},
        other_admin,
        Surface.MCP,
        services=state.services,
    )
    assert outcome.value["value"]["items"] == []
