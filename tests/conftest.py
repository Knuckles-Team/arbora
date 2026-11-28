"""Suite-wide isolation: no test reads the host's own configuration files."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_configuration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every configuration lookup at an empty per-test directory."""
    home = tmp_path / "config-home"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    monkeypatch.setenv("ARBORA_CONFIG_DIR", str(home / "arbora"))


@pytest.fixture(autouse=True)
def _bound_error_store() -> Iterator[None]:
    """Each test is a host entry point: it owns a fresh error store for its context."""
    from arbora.observability.state import Errors
    from arbora.security.error_surface import bind_errors, unbind_errors

    token = bind_errors(Errors())
    yield
    unbind_errors(token)


@pytest.fixture
def default_agent_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host has registered the default fleet agent: its library entry holds no skill.

    A run that names no agent is gated on the default agent's graph definition, so a test
    that drives ``agents.run`` without its own engine takes this entry from the host.
    """
    from arbora.api.ops import agent_library
    from arbora.core.agent_names import FLEET_AGENT_NAME

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


def _rebound_globals(module: object) -> tuple[str, ...]:
    """Names a module rebinds through ``global`` statements (its registration slots)."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            names.update(node.names)
    return tuple(sorted(names))


@pytest.fixture(autouse=True)
def _isolated_host_port_registrations() -> object:
    """A test's host-port registrations never outlive the test.

    ``meristem.host_ports`` and ``arbora.agent_runs`` keep one process-wide slot per port
    (checkpoint factory, decision provider, event sink, ...). A served test that composes
    the host binds them for the process, so a later unit test of a run saw a checkpoint
    factory it never registered and tried to start a local engine. Each test starts from
    the slots it found and restores them on exit.
    """

    from meristem import host_ports

    from arbora import agent_runs

    modules = [host_ports, agent_runs]
    saved = [
        (module, name, getattr(module, name))
        for module in modules
        for name in _rebound_globals(module)
        if hasattr(module, name)
    ]
    yield
    for module, name, value in saved:
        setattr(module, name, value)


@pytest.fixture(autouse=True)
def _admitting_release_gate(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Tests of other rows launch connectors against a gate that admits every release.

    ``real_release_gate`` keeps the real gate so the row's own tests drive record
    and verify through a recorded engine (ARBORA-HOST-R037.3).
    """
    if request.node.get_closest_marker("real_release_gate") is not None:
        yield
        return
    from arbora.fleet import release_gate
    from arbora.fleet.onboarding import FleetOnboarding

    class _Admits:
        async def admit(self, session: object, connector: str) -> dict[str, object]:
            return {"state": "unreported", "verified": True}

    async def admit_all(self: object, endpoint: object, *, auth: object) -> dict:
        return {"state": "unreported", "verified": True}

    monkeypatch.setattr(FleetOnboarding, "_admit_release", admit_all)
    release_gate.install_release_gate(_Admits)
    try:
        yield
    finally:
        release_gate.install_release_gate(None)
