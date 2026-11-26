"""Capability fields derive only from wired authorities (ARBORA-A2A-R014)."""

from __future__ import annotations

import pytest

from arbora.a2a.models import A2AAgentCapabilities
from arbora.a2a.service import A2AService
from tests.a2a._push_support import world
from tests.a2a.test_application import Authority, Router


@pytest.mark.spec("ARBORA-A2A-R014")
def test_capabilities_false_without_authorities() -> None:
    capabilities = A2AAgentCapabilities.from_wired_authorities()
    assert (
        capabilities.streaming,
        capabilities.push_notifications,
        capabilities.state_transition_history,
    ) == (False, False, False)
    card = A2AService(authority=Authority(), router=Router()).agent_card(
        "https://x/a2a"
    )
    assert card.capabilities == capabilities


@pytest.mark.spec("ARBORA-A2A-R014")
def test_no_bare_flag_forces_a_capability_and_a_wired_worker_does() -> None:
    with pytest.raises(TypeError):
        A2AAgentCapabilities.from_wired_authorities(
            push_notification_authority=object()  # type: ignore[arg-type]
        )
    wired = A2AAgentCapabilities.from_wired_authorities(
        push_notification_authority=world().worker
    )
    assert wired.push_notifications is True and wired.streaming is False
