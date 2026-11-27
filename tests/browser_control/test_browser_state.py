"""BrowserState owns the browser-control binding; two hosts share none (ARBORA-HOST-R039.6)."""

from __future__ import annotations

import asyncio
import threading

import pytest

from arbora.browser_control import browser_control_runtime
from arbora.browser_control.browser_state import BrowserState
from arbora.mcp_server import browser_dispatch
from arbora.mcp_server.host_context import build_host_context


class _Service:
    pass


def _run_on_own_loop(
    state: BrowserState, service: _Service
) -> asyncio.AbstractEventLoop:
    """Bind ``service`` to a loop running in its own thread and return that loop."""
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    bound = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(loop)

        def bind() -> None:
            state.bind_owner(service)
            bound.set()

        loop.call_soon(bind)
        loop.call_soon(ready.set)
        loop.run_forever()

    threading.Thread(target=serve, daemon=True).start()
    assert ready.wait(2.0) and bound.wait(2.0)
    return loop


@pytest.mark.spec("ARBORA-HOST-R039.6")
def test_two_browser_states_own_separate_owner_loops() -> None:
    first, second = build_host_context().browser, build_host_context().browser
    assert first is not second and first.capacity is not second.capacity
    one, two = _Service(), _Service()
    first.register(one)
    second.register(two)
    loop_one = _run_on_own_loop(first, one)
    loop_two = _run_on_own_loop(second, two)
    try:
        assert first.authority() == (one, loop_one)
        assert second.authority() == (two, loop_two)
        assert loop_one is not loop_two
        # Releasing one host's service leaves the other's attached.
        first.unregister(one)
        assert not first.attached() and second.attached()
        with pytest.raises(RuntimeError, match="not attached"):
            first.authority()
    finally:
        for loop in (loop_one, loop_two):
            loop.call_soon_threadsafe(loop.stop)
    # The binding moved off the modules: no service, loop, lock or capacity global.
    for name in ("_service", "_owner_loop", "_binding_lock"):
        assert not hasattr(browser_control_runtime, name)
    assert not hasattr(browser_dispatch, "_capacity")


def test_a_second_service_is_refused_until_the_first_is_released() -> None:
    state = BrowserState()
    first, second = _Service(), _Service()
    state.register(first)
    with pytest.raises(RuntimeError, match="different live"):
        state.register(second)
    with pytest.raises(RuntimeError, match="identity changed"):
        state.unregister(second)
    state.unregister(first)
    state.register(second)
