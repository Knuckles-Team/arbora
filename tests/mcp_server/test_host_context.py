"""HostContext owns run-time state; two contexts share none (ARBORA-HOST-R039)."""

from __future__ import annotations

import dataclasses

import pytest

from arbora import run_origin
from arbora.a2a import mcp as a2a_mcp
from arbora.api.contracts import use_session
from arbora.api.contracts.session import GraphSession
from arbora.mcp_server.host_context import HostContext, build_host_context
from tests.test_run_origin import session

pytestmark = pytest.mark.spec("ARBORA-HOST-R039")


def test_two_host_contexts_share_no_module_state() -> None:
    first = build_host_context(lambda: None)
    second = build_host_context(lambda: None)
    assert first.runs.origins is not second.runs.origins
    assert first.runs.a2a is not second.runs.a2a
    # The state moved off the modules: no registry or service global remains.
    assert not hasattr(run_origin, "_runs")
    assert not hasattr(a2a_mcp, "_SERVICE")
    assert not hasattr(a2a_mcp, "_service")


def test_host_context_is_frozen_and_slotted() -> None:
    host = build_host_context(lambda: None)
    assert dataclasses.is_dataclass(HostContext)
    with pytest.raises(dataclasses.FrozenInstanceError):
        host.runs = host.runs  # type: ignore[misc]
    assert not hasattr(host, "__dict__")


async def test_a_run_registered_on_one_context_is_invisible_to_the_other() -> None:
    first = build_host_context(lambda: None)
    second = build_host_context(lambda: None)
    admitted: GraphSession = session()
    with (
        use_session(admitted),
        first.runs.origins.agent_run_origin_scope(admitted, "web", "run"),
    ):
        assert len(first.runs.origins) == 1
        assert len(second.runs.origins) == 0
        second.runs.origins.revoke(admitted, "run")
        origin = run_origin._run.get()
        assert origin is not None and origin.is_live()
        first.runs.origins.revoke(admitted, "run")
        assert not origin.is_live()
    assert len(first.runs.origins) == 0
