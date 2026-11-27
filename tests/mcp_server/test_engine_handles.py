"""Two hosts hold two engine handles; no engine state lives on a module (ARBORA-HOST-R039.8)."""

from __future__ import annotations

import pytest

from arbora.api.domain.mcp_server.backing import engine_routing
from arbora.gateway import daemon
from arbora.mcp_server import served_engine
from arbora.mcp_server.backing.tools import engine_tools
from arbora.mcp_server.host_context import build_host_context
from arbora.mcp_server.served_engine import EngineHandles


@pytest.mark.spec("ARBORA-HOST-R039.8")
def test_two_engine_handles_hold_separate_pools() -> None:
    built: list[object] = []

    def factory() -> object:
        pool = object()
        built.append(pool)
        return pool

    first = EngineHandles(factory)
    second = EngineHandles(factory)
    assert first.pool() is first.pool()
    assert first.pool() is not second.pool()
    assert first.served() is first.served()
    assert first.served() is not second.served()
    assert len(built) == 2
    assert build_host_context().engine is not build_host_context().engine


@pytest.mark.spec("ARBORA-HOST-R039.8")
def test_no_module_holds_engine_state() -> None:
    for module, names in (
        (served_engine, ("_POOL", "_ENGINE", "_LOCK")),
        (engine_tools, ("_CLIENT_POOL", "_CLIENT_POOL_LOCK")),
        (daemon, ("_engine", "_lock")),
        (
            engine_routing,
            (
                "_EXTRACTION_MANAGER",
                "_CONNECTION_REGISTRY",
                "_REGISTRY_LOCK",
                "_TABULAR_QUERY_SERVICE",
            ),
        ),
    ):
        for name in names:
            assert not hasattr(module, name), f"{module.__name__}.{name}"
