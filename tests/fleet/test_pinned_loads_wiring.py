"""Pin and unpin are served manage operations; they add no resident tool.

ARBORA-FLEET-R059.4: the ops ride the existing ``manage`` intent tool with
scope ``mcp:delegate``, effect ``WRITE``, audit ``EVENT`` and the MCP-only
surface; ``RESIDENT_NAMES`` stays the ten of ``ARBORA-FLEET-R018`` and
``FLEET_OPERATIONS`` gains no entry.
"""

from __future__ import annotations

import pytest

from arbora.api.mcp.registration import FLEET_OPERATIONS, RESIDENT_NAMES
from arbora.api.ops.fleet import FleetPinParams, FleetPinsListParams, operations
from arbora.api.registry import AuditClass, Effect, Registry, Surface, Verb


@pytest.mark.spec("ARBORA-FLEET-R059.4")
def test_pin_op_is_a_manage_op_and_adds_no_resident_tool() -> None:
    registry = Registry(operations())
    pin = registry.get("fleet.tools.pin")
    unpin = registry.get("fleet.tools.unpin")
    assert pin is not None and unpin is not None
    for spec in (pin, unpin):
        assert spec.verb == Verb.MANAGE
        assert spec.effect == Effect.WRITE
        assert spec.scopes == frozenset({"mcp:delegate"})
        assert spec.surfaces == frozenset({Surface.MCP})
        assert spec.audit == AuditClass.EVENT
        assert spec.params is FleetPinParams
    pins = registry.get("fleet.pins.list")
    assert pins is not None
    assert pins.verb == Verb.ASK
    assert pins.effect == Effect.READ
    assert pins.scopes == frozenset({"fleet:control", "mcp:admin"})
    assert pins.audit == AuditClass.NONE
    assert pins.params is FleetPinsListParams
    # the family stays the four resident fleet tools: nothing resident added
    assert len(RESIDENT_NAMES) == 10
    assert set(FLEET_OPERATIONS) == {
        "find_tools",
        "load_tools",
        "unload_tools",
        "multiplexer_status",
    }
    assert not ({"pin", "unpin", "pins"} & set(RESIDENT_NAMES))
