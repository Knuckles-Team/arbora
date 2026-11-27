"""Two process authorities hold separate sessions and stop events (ARBORA-HOST-R039.9)."""

from __future__ import annotations

import threading

import pytest

from arbora.ecosystem import ard_signing
from arbora.mcp_server import bootstrap, runtime
from arbora.mcp_server.host_context import build_host_context
from arbora.mcp_server.served_engine import EngineHandles
from arbora.security import request_identity, run_token
from arbora.security.process_authority import ProcessAuthority


@pytest.mark.spec("ARBORA-HOST-R039.9")
def test_two_authorities_hold_separate_sessions_and_stop_events() -> None:
    first, second = ProcessAuthority(), ProcessAuthority()
    first.bind_session("session-one")
    assert first.session() == "session-one"
    assert second.session() is None
    assert first.stop is not second.stop
    assert first.refresh_lock is not second.refresh_lock
    first.stop.set()
    assert not second.stop.is_set()
    thread = threading.Thread(target=lambda: None)
    first.bind_thread(thread)
    assert second.take_thread() is None
    assert first.take_thread() is thread
    assert first.take_thread() is None
    assert build_host_context().authority is not build_host_context().authority


@pytest.mark.spec("ARBORA-HOST-R039.9")
def test_the_host_authority_is_the_tool_table_authority() -> None:
    host = build_host_context()
    assert host.authority is host.caches.tools.authority
    host.authority.bind_session("bound")
    handles = EngineHandles(lambda: object(), host.authority.session)
    assert handles.served().process_session() == "bound"


@pytest.mark.spec("ARBORA-HOST-R039.9")
def test_no_module_holds_process_authority_state() -> None:
    for module, names in (
        (runtime, ("_PROCESS_SESSION", "_PROCESS_SESSION_REFRESH_LOCK")),
        (runtime, ("_PROCESS_AUTHORITY_STOP", "_PROCESS_AUTHORITY_THREAD")),
        (bootstrap, ("_process_session", "set_process_session")),
        (bootstrap, ("_PROCESS_AUTHORITY_STOP", "_PROCESS_SESSION_REFRESH_LOCK")),
        (request_identity, ("_system_write_session", "_system_write_session_lock")),
        (run_token, ("_EPHEMERAL",)),
        (ard_signing, ("_EPHEMERAL_SEED",)),
    ):
        for name in names:
            assert not hasattr(module, name), f"{module.__name__}.{name}"
