"""Suite-wide isolation: no test reads the host's own configuration files."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every configuration lookup at an empty per-test directory."""
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(home / "arbora"))


@pytest.fixture
def default_agent_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host has registered the default fleet agent: its library entry holds no skill.

    A run that names no agent is gated on the default agent's graph definition, so a test
    that drives ``agents.run`` without its own engine takes this entry from the host.
    """
    from arbora.api.ops import agent_library
    from arbora.host_bindings import FLEET_AGENT_NAME

    real = agent_library.current_entry

    async def current(context: object, agent_id: str) -> dict[str, object]:
        if agent_id == FLEET_AGENT_NAME:
            return {"agent_id": agent_id, "skills": []}
        return await real(context, agent_id)

    monkeypatch.setattr(agent_library, "current_entry", current)


@pytest.fixture(autouse=True)
def _granting_run_capacity(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tests of other rows admit runs against an engine cell that always grants.

    ``real_run_capacity`` keeps the real port so the row's own tests drive the engine
    methods through a recorded engine.
    """
    if request.node.get_closest_marker("real_run_capacity") is not None:
        return
    from arbora.api.ops.run_capacity import CapacityReservation, EngineRunCapacity

    async def reserve(self, context, *, run_id, work_item_id, owner_digest):
        return CapacityReservation(
            cell_id="run:agents",
            lease_id=f"lease-{run_id}",
            lease_epoch=1,
            fence_token=1,
        )

    async def release(self, context, reservation, *, owner_digest):
        return None

    monkeypatch.setattr(EngineRunCapacity, "reserve", reserve)
    monkeypatch.setattr(EngineRunCapacity, "release", release)
