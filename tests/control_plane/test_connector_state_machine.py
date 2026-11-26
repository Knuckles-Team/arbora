"""Connector desired state is a machine; authorization is a projection (ARBORA-FLEET-R082)."""

from __future__ import annotations

import inspect
import re

import pytest

from arbora.control_plane.connectors import machine, service
from arbora.control_plane.connectors.machine import (
    DESIRED_EDGES,
    DESIRED_STATES,
    DesiredEvent,
    IllegalDesiredTransition,
    project_outcome,
    transition,
)
from arbora.control_plane.connectors.repository import RepositoryContractError
from tests.control_plane.test_connectors import _decision, _fixture

_LEGAL = [(s, e, t) for (s, e), t in DESIRED_EDGES.items()]


@pytest.mark.spec("ARBORA-FLEET-R082.1")
@pytest.mark.parametrize(
    "edge", _LEGAL, ids=[f"{s}-{e.value}-{t}" for s, e, t in _LEGAL]
)
def test_legal_edge(edge) -> None:
    source, event, target = edge
    assert transition(source, event) == target


@pytest.mark.spec("ARBORA-FLEET-R082.1")
def test_every_illegal_pair_is_refused() -> None:
    legal = {(s, e) for s, e, _ in _LEGAL}
    illegal = [
        (s, e) for s in DESIRED_STATES for e in DesiredEvent if (s, e) not in legal
    ]
    assert len(illegal) == 7
    for source, event in illegal:
        with pytest.raises(IllegalDesiredTransition) as refused:
            transition(source, event)
        assert refused.value.code == "CONNECTOR_TRANSITION_ILLEGAL"
        assert refused.value.machine == "connector_desired_status"
        assert (refused.value.from_state, refused.value.event) == (source, event)


@pytest.mark.spec("ARBORA-FLEET-R082.2")
@pytest.mark.parametrize("state", ["pending", "approved", "denied", "revoked"])
def test_projection_maps_each_decision_state(state: str) -> None:
    assert project_outcome(state) == state
    with pytest.raises(ValueError):
        project_outcome("unknown-state")


@pytest.mark.spec("ARBORA-FLEET-R082")
@pytest.mark.spec("ARBORA-FLEET-R082.2")
def test_denied_never_becomes_approved_without_a_new_decision() -> None:
    _, plane, scope, registration = _fixture()
    plane.register(registration)
    denied = _decision(registration, scope, "approval", outcome="denied")
    plane.authorize(denied, scope=scope)
    approved_again = denied.model_copy(update={"outcome": "approved"})
    with pytest.raises(RepositoryContractError):
        plane.authorize(approved_again, scope=scope)
    fresh = approved_again.model_copy(update={"decision_id": "decision:approval:2"})
    plane.authorize(fresh, scope=scope)


@pytest.mark.spec("ARBORA-FLEET-R082.3")
def test_served_quarantine_cannot_be_enabled_directly() -> None:
    _, plane, scope, registration = _fixture()
    plane.register(registration)
    quarantine = registration.desired.model_copy(
        update={"desired_status": "quarantined", "revision": 2}
    )
    plane.set_desired(scope, quarantine)
    enable = quarantine.model_copy(update={"desired_status": "enabled", "revision": 3})
    with pytest.raises(IllegalDesiredTransition):
        plane.set_desired(scope, enable)
    release = quarantine.model_copy(
        update={"desired_status": "disabled", "revision": 3}
    )
    plane.set_desired(scope, release)


@pytest.mark.spec("ARBORA-FLEET-R082.3")
def test_no_site_assigns_desired_status_outside_the_machine() -> None:
    source = inspect.getsource(service) + inspect.getsource(machine)
    assert not re.findall(r"\.desired_status\s*=[^=]", source)
