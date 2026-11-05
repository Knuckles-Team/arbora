"""Pin authority: loadable only what the caller may load; stable refusals.

ARBORA-FLEET-R059.5 and R059.6: a principal pins only what it may already
load, the pin never stores a token (every call takes the fleet.call path
with the caller's delegated credential), and each refusal keeps its stable
code and HTTP status.
"""

from __future__ import annotations

import dataclasses

import pytest

from arbora.api.errors import _ARBORA_STATUS, ArboraErrorCode
from arbora.api.invoke import invoke
from arbora.api.registry import Effect, Surface
from tests.fleet._pin_harness import harness, pin

#: The stable HTTP status each refusal code keeps (ARBORA-FLEET-R059.6).
_EXPECTED_STATUS = {
    "SCOPE_REQUIRED": 403,
    "NOT_FOUND": 404,
    "INVALID_ARGUMENT": 400,
    "POLICY_DENIED": 403,
    "POLICY_UNAVAILABLE": 503,
    "UNAVAILABLE": 503,
    "LOAD_CAP_EXCEEDED": 409,
}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.5")
async def test_pin_needs_load_authority_and_call_uses_caller_credential() -> None:
    state = await harness()
    ops = state.ops

    # bob lacks the item scope: to him the item is missing, so the pin is
    # refused exactly as a load would be
    outcome = await pin(state, state.bob, ("fleet:tool:s/secret",))
    assert outcome.code == "NOT_FOUND"
    assert await state.store.list("tenant", "bob") == ()

    # alice pins the item she may load
    outcome = await pin(state, state.alice, ("fleet:tool:s/secret",))
    assert outcome.code == "OK"
    assert outcome.value["value"]["pinned"] == ["fleet:tool:s/secret"]

    # the durable record stores the binding only: no token, no credential
    record = (await state.store.list("tenant", "alice"))[0]
    assert record.principal == "alice"
    assert record.tenant == "tenant"
    assert record.binding == ("fleet:tool:s/secret", "tool", "s", "secret")
    assert {field.name for field in dataclasses.fields(record)} == {
        "tenant",
        "principal",
        "item_id",
        "binding",
        "pinned_at_ms",
    }

    # a call to the pinned tool takes the fleet.call path with her own
    # delegated credential
    await state.gateway.call(
        state.alice, "s", "secret", {}, expected_effect=Effect.WRITE
    )
    server, tool, arguments, caller = state.delegated_calls[-1]
    assert (server, tool, arguments) == ("s", "secret", {})
    assert caller is state.alice

    # the policy revokes the item: the pin stays stored but suspends, and
    # the next call refuses
    state.policy_ok = False
    status = await ops.status(state.alice)
    assert status["suspended"] == [
        {"item_id": "fleet:tool:s/secret", "code": "POLICY_DENIED"}
    ]
    assert await state.store.list("tenant", "alice")
    with pytest.raises(PermissionError):
        await ops.admitted_tool(state.alice, "s", "secret")


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.6")
async def test_pin_refusals_carry_stable_codes() -> None:
    state = await harness(pinned_cap=1)

    # SCOPE_REQUIRED: the op's scope is enforced before dispatch
    outcome = await pin(state, state.carol, ("fleet:tool:s/read",))
    assert outcome.code == "SCOPE_REQUIRED"
    assert "mcp:delegate" in outcome.details.get("missing_scopes", ())

    # NOT_FOUND: an item outside the catalog
    outcome = await pin(state, state.bob, ("fleet:tool:s/ghost",))
    assert outcome.code == "NOT_FOUND"

    # INVALID_ARGUMENT: more than the 32-item bound in one request
    outcome = await pin(
        state, state.alice, tuple(f"fleet:tool:s/t{i}" for i in range(33))
    )
    assert outcome.code == "INVALID_ARGUMENT"

    # POLICY_DENIED: the load policy says no
    state.policy_ok = False
    outcome = await pin(state, state.alice, ("fleet:tool:s/read",))
    assert outcome.code == "POLICY_DENIED"

    # POLICY_UNAVAILABLE: the policy decision is unreachable
    state.policy_ok = True
    state.policy_raising = True
    outcome = await pin(state, state.alice, ("fleet:tool:s/read",))
    assert outcome.code == "POLICY_UNAVAILABLE"

    # UNAVAILABLE: no durable pin store is composed
    bare = await harness(pinned_cap=1, pin_store=None)
    outcome = await pin(bare, bare.alice, ("fleet:tool:s/read",))
    assert outcome.code == "UNAVAILABLE"

    # LOAD_CAP_EXCEEDED with cap: pinned
    state.policy_raising = False
    outcome = await pin(
        state, state.alice, ("fleet:tool:s/read", "fleet:tool:s/secret")
    )
    assert outcome.code == "LOAD_CAP_EXCEEDED"
    assert outcome.details.get("cap") == "pinned"

    # the pins failed, so nothing is stored and the live session is unchanged
    assert await state.store.list("tenant", "alice") == ()
    assert state.ops.sessions.loaded("tenant:alice") == frozenset()

    # every code above keeps its stable HTTP status
    for code, status_code in _EXPECTED_STATUS.items():
        assert _ARBORA_STATUS[ArboraErrorCode(code)] == status_code


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-FLEET-R059.6")
async def test_unpin_reports_not_pinned_items() -> None:
    state = await harness()
    alice = state.alice

    outcome = await invoke(
        "fleet.tools.unpin",
        {"items": ["fleet:tool:s/read", "fleet:tool:s/ghost"]},
        alice,
        Surface.MCP,
        services=state.services,
        idempotency_key="unpin:one",
    )
    assert outcome.code == "OK"
    assert outcome.value["value"] == {
        "unpinned": [],
        "not_pinned": ["fleet:tool:s/ghost", "fleet:tool:s/read"],
        "session_total": 0,
    }
