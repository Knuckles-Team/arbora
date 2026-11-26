# ruff: noqa: F811
"""Every onboarding trigger reaches the one onboard function (ARBORA-FLEET-R041.3)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.fleet import onboarding
from arbora.fleet.onboarding import (
    FleetEndpoint,
    FleetOnboarding,
    FleetOnboardingService,
    OnboardingTrigger,
    onboard_fleet,
)
from tests.fleet.test_fleet_onboarding import (
    ALPHA,
    _Mux,
    _onboarding,
    _Registry,
    _run_in_session,
    eg,  # noqa: F401
)


@pytest.mark.spec("ARBORA-FLEET-R041.3")
def test_every_trigger_reaches_the_one_onboard_function(
    eg: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``onboard_fleet``, ``run_pass`` and the handshake pass share one ``onboard``."""

    calls: list[tuple[str, str]] = []

    async def recording_onboard(
        _self: FleetOnboarding,
        endpoint: FleetEndpoint,
        *,
        auth: Any,
        trigger: OnboardingTrigger | None = None,
    ) -> bool:
        calls.append((endpoint.name, trigger.reason if trigger else "default"))
        return False

    monkeypatch.setattr(FleetOnboarding, "onboard", recording_onboard)
    monkeypatch.setattr(
        onboarding, "configured_fleet_endpoints", lambda **_kwargs: (ALPHA,)
    )

    async def drive() -> None:
        await onboard_fleet(_onboarding(eg), (ALPHA,))
        await FleetOnboardingService(
            onboarding=_onboarding(eg), multiplexer=_Mux({})
        ).run_pass()
        for reason in ("handshake", "list_changed"):
            await _onboarding(eg).handshake_pass(
                ALPHA,
                auth=None,
                trigger=OnboardingTrigger(ALPHA.name, reason, f"digest-{reason}"),
                server_version="1",
            )

    _run_in_session(drive())

    assert calls == [
        (ALPHA.name, "default"),
        (ALPHA.name, "default"),
        (ALPHA.name, "handshake"),
        (ALPHA.name, "list_changed"),
    ]
