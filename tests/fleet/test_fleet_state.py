"""Two fleet states share no registry (ARBORA-HOST-R039.2)."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest

from arbora.fleet import (
    access_contracts,
    child_resilience,
    gateway_ops,
    multiplexer,
    plan_confirmation,
)
from arbora.fleet.plan_confirmation import confirmed_plan_call
from arbora.fleet.state import FleetState
from arbora.mcp_server.host_context import build_host_context
from tests.fleet.catalog_fixture import multiplexer_from_fixture

pytestmark = pytest.mark.spec("ARBORA-HOST-R039.2")

TOOL = "servicenow_delete_table_record"


class _Session:
    """A child session stand-in; arming keys on it weakly, as on a real one."""


def _confirmation_request() -> SimpleNamespace:
    return SimpleNamespace(
        message="Are you sure you want to delete_table_record?",
        requested_schema={
            "type": "object",
            "properties": {"confirmed": {"type": "boolean"}},
        },
    )


async def test_two_fleet_states_share_no_registry(tmp_path) -> None:
    first, second = FleetState(), FleetState()
    assert first.multiplexers is not second.multiplexers
    assert first.oauth_brokers is not second.oauth_brokers
    assert first.confirmations is not second.confirmations
    assert first.confirming_tools is not second.confirming_tools
    assert first.virtual_catalog is not second.virtual_catalog
    # The registries moved off the modules: no fleet global remains.
    for module, name in (
        (multiplexer, "_LIVE_MULTIPLEXERS"),
        (multiplexer, "_REMOTE_OAUTH_BROKERS"),
        (multiplexer, "_REMOTE_OAUTH_BROKERS_LOCK"),
        (plan_confirmation, "_ARMED"),
        (plan_confirmation, "_LOCKS"),
        (gateway_ops, "_CONFIRMING_TOOLS"),
        (access_contracts, "_CATALOG"),
    ):
        assert not hasattr(module, name), name
    assert not hasattr(child_resilience, "armed")

    # A session armed on one fleet is never confirmed by the other's callback.
    session = _Session()
    context = SimpleNamespace(session=session)
    with confirmed_plan_call(TOOL):
        async with first.confirmations.armed(session, TOOL):
            declined = await second.confirmations.answer_elicitation(
                context, _confirmation_request()
            )
            accepted = await first.confirmations.answer_elicitation(
                context, _confirmation_request()
            )
    assert declined.action == "decline"
    assert accepted.action == "accept"

    # A tool one fleet learned asks for confirmation is unknown to the other.
    from arbora.api.errors import FleetRefusal

    with pytest.raises(FleetRefusal):
        first.confirming_tools.refuse_cancelled({"cancelled": True}, "child", "drop")
    assert ("child", "drop") in first.confirming_tools
    assert ("child", "drop") not in second.confirming_tools

    # The multiplexer registers in its own fleet only.
    mux = multiplexer_from_fixture(tmp_path / "c.json")
    assert mux.fleet is not first and mux.fleet is not second


def test_the_host_context_builds_one_fleet_state_per_context() -> None:
    one = build_host_context(lambda: None)
    other = build_host_context(lambda: None)
    assert isinstance(one.fleet, FleetState)
    assert one.fleet is not other.fleet
    field = dataclasses.fields(FleetState)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(one.fleet, field, getattr(other.fleet, field))
