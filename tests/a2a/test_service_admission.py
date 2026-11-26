"""Admission runs on the send path before any dispatch (ARBORA-A2A-R017.5, R016.4)."""

from __future__ import annotations

import pytest

from arbora.a2a.service import A2AService
from tests.a2a._admission_support import Scripted, part, record
from tests.a2a._service_support import (
    CapabilityRouter,
    RecordingAuthority,
    client,
    send,
)
from tests.a2a.test_application import Authority, Router


def _service(decider, authority=None, router=None):  # type: ignore[no-untyped-def]
    return A2AService(
        authority=authority or RecordingAuthority(),
        router=router or CapabilityRouter(),
        harness_decider=decider,
        principal_kind=lambda: "human",
    )


@pytest.mark.spec("ARBORA-A2A-R017.5")
def test_send_message_refuses_without_admission() -> None:
    cases = {
        "no decider": (None, CapabilityRouter(), "HARNESS_CONFIG_UNAVAILABLE"),
        "unreachable": (
            Scripted(OSError("x")),
            CapabilityRouter(),
            "HARNESS_CONFIG_UNAVAILABLE",
        ),
        "abstained": (
            Scripted(
                record(tool_subset=part(conservative_default=[], missing_fact=["f"]))
            ),
            CapabilityRouter(),
            "HARNESS_CONFIG_ABSTAINED",
        ),
        "no capability": (
            Scripted(),
            CapabilityRouter(task_class=None),
            "HARNESS_CONFIG_UNAVAILABLE",
        ),
    }
    for label, (decider, router, reason) in cases.items():
        authority = RecordingAuthority()
        response = send(client(_service(decider, authority, router)))
        error = response.json()["error"]
        assert (response.status_code, error["code"]) == (503, -32003), label
        assert error["data"] == {"reason": reason}, label
        assert authority.dispatched == [], label  # no WorkItem is enqueued


@pytest.mark.spec("ARBORA-A2A-R017.5")
def test_a_message_without_budget_keeps_the_default_agent_path() -> None:
    decider = Scripted()
    authority = RecordingAuthority()
    response = send(client(_service(decider, authority)), budget=None)
    assert response.status_code == 200
    assert decider.calls == [] and "harness" not in authority.dispatched[0]
    # A service with no router capability and no budget needs no admission wiring.
    plain = A2AService(authority=Authority(), router=Router())
    assert send(client(plain), budget=None).status_code == 200
