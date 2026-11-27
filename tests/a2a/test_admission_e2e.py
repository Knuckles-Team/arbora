"""An admitted task reaches the dispatch with the engine's harness config (ARBORA-A2A-R017.6, R018.1).

``message/send`` with a context budget runs the router, the scripted decider and the
authority double. The one dispatch carries the admitted answer, and the card still
advertises no harness field (ARBORA-A2A-R014.1).
"""

from __future__ import annotations

import pytest

from arbora.a2a.authority import harness_metadata
from arbora.a2a.service import A2AService
from arbora.core.settings import RuntimeConfig
from tests.a2a._admission_support import DIGEST, TOOLS, Scripted
from tests.a2a._service_support import (
    CapabilityRouter,
    RecordingAuthority,
    client,
    send,
)


@pytest.mark.spec("ARBORA-A2A-R017.6")
def test_message_send_enqueues_envelope_with_harness_config() -> None:
    decider = Scripted()
    authority = RecordingAuthority()
    service = A2AService(
        authority=authority,
        router=CapabilityRouter(),
        harness_decider=decider,
        principal_kind=lambda: "service",
        config=RuntimeConfig(),
    )
    assert send(client(service)).status_code == 200
    assert len(decider.calls) == 1 and decider.calls[0]["priority"] == "orchestration"
    assert decider.calls[0]["budget_tokens"] == 4_096
    [dispatched] = authority.dispatched
    harness = dispatched["harness"]
    assert harness.admitted_tools == tuple(TOOLS)
    assert harness.harness_config == decider.answer.answer
    assert harness_metadata(harness) == {
        "decision_record_ref": "record:harness:1",
        "harness_config_digest": DIGEST,
        "allowed_tool_subset": TOOLS,
        "context_budget_tokens": 4_096,
    }


@pytest.mark.spec("ARBORA-A2A-R017.6")
def test_a_principal_without_a_priority_class_is_refused_before_the_engine() -> None:
    decider = Scripted()
    service = A2AService(
        authority=RecordingAuthority(),
        router=CapabilityRouter(),
        harness_decider=decider,
        principal_kind=lambda: "agent",
        config=RuntimeConfig(),
    )
    response = send(client(service))
    assert response.status_code in (401, 403)
    assert decider.calls == []
