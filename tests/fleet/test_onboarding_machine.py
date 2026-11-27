"""Connector onboarding handshake is an explicit state machine (ARBORA-FLEET-R080)."""

from __future__ import annotations

import inspect
import re
import threading
from typing import Any

import pytest

from arbora.fleet import onboarding_service as onboarding_module
from arbora.fleet.onboarding_capture import FleetEndpoint
from arbora.fleet.onboarding_machine import (
    ONBOARDING_EDGES,
    IllegalOnboardingTransition,
    OnboardingEvent,
    OnboardingState,
    transition,
)
from arbora.fleet.onboarding_service import SLOW_RETRY_S, HandshakeOnboarder
from tests.fleet.test_handshake_onboard import (  # noqa: F401  (eg is a fixture)
    DEBOUNCE,
    _handshake,
    _record,
    _served_pack,
    _setup,
    eg,
)

_LEGAL = [
    (s, e, guard, value, t) for (s, e, guard, value), t in ONBOARDING_EDGES.items()
]


def _kwargs(guard: str | None, value: bool) -> dict[str, bool]:
    return (
        {"attempts_left": value}
        if guard == "attempts_left"
        else ({"lease_live": value} if guard == "lease_live" else {})
    )


@pytest.mark.spec("ARBORA-FLEET-R080.1")
@pytest.mark.parametrize(
    "edge",
    _LEGAL,
    ids=[
        f"{s.value}-{e.value}-{t.value}".replace("_", "_") for s, e, _, _, t in _LEGAL
    ],
)
def test_legal_edge(edge) -> None:
    source, event, guard, value, target = edge
    assert transition(source, event, **_kwargs(guard, value)) is target


@pytest.mark.spec("ARBORA-FLEET-R080.1")
def test_every_illegal_pair_is_refused() -> None:
    legal = {(s, e) for s, e, *_ in _LEGAL}
    for source in OnboardingState:
        for event in OnboardingEvent:
            if (source, event) in legal:
                continue
            with pytest.raises(IllegalOnboardingTransition) as refused:
                transition(source, event)
            assert refused.value.code == "ONBOARDING_TRANSITION_ILLEGAL"
            assert refused.value.machine == "fleet_onboarding"
            assert (refused.value.from_state, refused.value.event) == (source, event)
    # A legal pair whose guard fails is refused too.
    with pytest.raises(IllegalOnboardingTransition):
        transition(
            OnboardingState.FAILED, OnboardingEvent.AUTHORITY_LIVE, lease_live=False
        )


@pytest.mark.spec("ARBORA-FLEET-R080.2")
@pytest.mark.spec("ARBORA-FLEET-R080")
def test_identity_provider_blip_does_not_strand_connectors(
    eg,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = threading.Event()
    waits: list[float] = []
    calls = 0

    async def capture(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
        nonlocal calls
        calls += 1
        if not live.is_set():
            raise ConnectionError("identity provider unavailable")
        return await _served_pack(endpoint, auth=auth)

    onboarder, mux, clock = _setup(eg, capture, max_attempts=2, retry_base_s=1.0)
    monkeypatch.setattr(HandshakeOnboarder, "_lease_live", lambda self: live.is_set())

    def wait(seconds: float) -> bool:
        waits.append(seconds)
        if seconds == DEBOUNCE:
            clock.gate.wait(10)
        elif (
            seconds >= SLOW_RETRY_S
            and len([w for w in waits if w >= SLOW_RETRY_S]) == 3
        ):
            live.set()  # the lease recovers after three lifetimes
        return False

    onboarder.wait = wait
    _handshake(mux)
    clock.gate.set()
    onboarder.drain()
    assert len([w for w in waits if w >= SLOW_RETRY_S]) == 3
    assert _record(mux)["state"] == OnboardingState.IDLE.value
    assert _record(mux)["outcome"] == "onboarded"


@pytest.mark.spec("ARBORA-FLEET-R080.4")
def test_handshake_to_status_runs_through_the_machine(
    eg,  # noqa: F811 - the imported fixture
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[OnboardingEvent] = []
    real = onboarding_module.transition

    def recording(current, event, **kwargs):
        events.append(event)
        return real(current, event, **kwargs)

    monkeypatch.setattr(onboarding_module, "transition", recording)
    onboarder, mux, clock = _setup(eg)
    _handshake(mux)
    clock.gate.set()
    onboarder.drain()
    assert events == [
        OnboardingEvent.REGISTERED,
        OnboardingEvent.DEBOUNCE_ELAPSED,
        OnboardingEvent.PASS_OK,
    ]
    assert _record(mux)["state"] == "idle"


@pytest.mark.spec("ARBORA-FLEET-R080.4")
def test_no_site_writes_a_state_string() -> None:
    source = inspect.getsource(onboarding_module)
    assert not re.findall(r'_record\([^)]*"(?:failed|awaiting_approval)"', source)
    assert len(re.findall(r'\["state"\]\s*=|"state":\s*+(?!state\.value)', source)) == 0
    assert source.count("= transition(") == 1  # _record is its only caller
