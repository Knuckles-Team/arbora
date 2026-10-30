"""Tests for the ARBORA-HOST-R015 layered agent-capability model."""

from __future__ import annotations

import pytest

from arbora.architecture.capability_layers import (
    CapabilityLayer,
    InProcessHarness,
    UnsupportedCapabilityLayerError,
)

pytestmark = pytest.mark.spec("ARBORA-HOST-R015")


def test_layers_are_ordered_lowest_to_highest() -> None:
    ordered = sorted(CapabilityLayer)
    assert ordered == [
        CapabilityLayer.SINGLE_TOOL_CALL,
        CapabilityLayer.SINGLE_AGENT_TASK,
        CapabilityLayer.MULTI_STEP_PLANNING,
        CapabilityLayer.MULTI_AGENT_ORCHESTRATION,
        CapabilityLayer.AUTONOMOUS_WORKFLOW,
    ]


def test_harness_runs_a_layer_it_declares_support_for() -> None:
    harness = InProcessHarness(
        name="pilot",
        handlers={CapabilityLayer.SINGLE_TOOL_CALL: lambda: "ok"},
    )
    run = harness.run(CapabilityLayer.SINGLE_TOOL_CALL)
    assert run.result == "ok"
    assert run.harness_name == "pilot"
    assert run.layer is CapabilityLayer.SINGLE_TOOL_CALL


def test_harness_refuses_a_layer_it_does_not_support() -> None:
    harness = InProcessHarness(
        name="pilot",
        handlers={CapabilityLayer.SINGLE_TOOL_CALL: lambda: "ok"},
    )
    with pytest.raises(UnsupportedCapabilityLayerError):
        harness.run(CapabilityLayer.MULTI_AGENT_ORCHESTRATION)


def test_supported_layers_reflects_declared_handlers_only() -> None:
    harness = InProcessHarness(
        name="pilot",
        handlers={
            CapabilityLayer.SINGLE_TOOL_CALL: lambda: None,
            CapabilityLayer.SINGLE_AGENT_TASK: lambda: None,
        },
    )
    assert harness.supported_layers == frozenset(
        {CapabilityLayer.SINGLE_TOOL_CALL, CapabilityLayer.SINGLE_AGENT_TASK}
    )


@pytest.mark.parametrize("layer", list(CapabilityLayer))
@pytest.mark.parametrize("environment", ["local", "sandbox"])
def test_every_layer_runs_through_any_harness_environment(
    layer: CapabilityLayer, environment: str
) -> None:
    """Each defined layer runs through the harness abstraction in more than
    one execution environment, and stays inside its own behaviour boundary."""
    harness = InProcessHarness(
        name=environment,
        handlers={layer: lambda: (environment, layer)},
    )
    run = harness.run(layer)
    assert run.result == (environment, layer)
    assert run.layer is layer
    assert run.harness_name == environment
    for other in CapabilityLayer:
        if other is not layer:
            with pytest.raises(UnsupportedCapabilityLayerError):
                harness.run(other)
