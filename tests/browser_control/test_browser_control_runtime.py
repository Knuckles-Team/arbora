"""Same-instance and authorization tests for the browser-control caller adapter."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from arbora.api.domain.browser_control.browser_control_api import (
    BrowserLeaseReceipt,
    BrowserLeaseStatus,
)
from arbora.browser_control.browser_control_runtime import (
    dispatch_browser_control,
)
from arbora.browser_control.browser_state import BrowserState
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.session import (
    GraphSession,
    current_session,
    use_session,
)

_BROWSER = BrowserState()


class _Service:
    def __init__(self) -> None:
        self.seen_session: GraphSession | None = None

    async def issue_lease(self, request: Any) -> BrowserLeaseReceipt:
        self.seen_session = current_session()
        return BrowserLeaseReceipt(
            lease_id="browserlease_0123456789abcdef0123456789abcdef",
            document_ref=request.document_ref,
            tool_ids=request.tool_ids,
            registration_generation=1,
            expires_at=100.0,
            hard_expires_at=200.0,
            status=BrowserLeaseStatus.ACTIVE,
        )


@pytest.fixture
def session() -> GraphSession:
    actor = ActorContext(
        actor_id="browser-user",
        actor_type=ActorType.HUMAN,
        roles=("user",),
        tenant_id="tenant-a",
        authenticated=True,
    )
    return GraphSession(
        actor=actor,
        tenant="tenant-a",
        scopes=frozenset({"kg:write"}),
        policy_version="policy-7",
        audience="graph-runtime",
    )


@pytest.mark.asyncio
async def test_foreign_loop_dispatch_preserves_verified_session(
    session: GraphSession,
) -> None:
    service = _Service()
    owner_loop = asyncio.new_event_loop()
    ready = threading.Event()
    bound = threading.Event()

    def serve() -> None:
        asyncio.set_event_loop(owner_loop)
        owner_loop.call_soon(ready.set)
        owner_loop.run_forever()

    thread = threading.Thread(target=serve, daemon=True)
    _BROWSER.register(service)
    thread.start()
    assert await asyncio.to_thread(ready.wait, 1.0)

    def bind() -> None:
        _BROWSER.bind_owner(service)
        bound.set()

    owner_loop.call_soon_threadsafe(bind)
    assert await asyncio.to_thread(bound.wait, 1.0)
    try:
        with use_session(session):
            receipt = await dispatch_browser_control(
                _BROWSER,
                "issue_lease",
                {
                    "document_ref": "document_" + "a" * 64,
                    "tool_ids": ["overstory.get-page-context"],
                    "attended": True,
                },
            )
        assert isinstance(receipt, BrowserLeaseReceipt)
        assert receipt.status == "active"
        assert service.seen_session == session
    finally:
        _BROWSER.unregister(service)
        owner_loop.call_soon_threadsafe(owner_loop.stop)
        await asyncio.to_thread(thread.join, 1.0)
        owner_loop.close()


@pytest.mark.asyncio
async def test_runtime_refuses_replacement_and_unbound_caller(
    session: GraphSession,
) -> None:
    first = _Service()
    second = _Service()
    _BROWSER.register(first)
    try:
        with pytest.raises(RuntimeError, match="different live"):
            _BROWSER.register(second)
        with use_session(session), pytest.raises(RuntimeError, match="not attached"):
            await dispatch_browser_control(
                _BROWSER,
                "issue_lease",
                {
                    "document_ref": "document_" + "a" * 64,
                    "tool_ids": ["overstory.get-page-context"],
                    "attended": True,
                },
            )
    finally:
        _BROWSER.unregister(first)


@pytest.mark.asyncio
async def test_runtime_requires_write_scope(session: GraphSession) -> None:
    service = _Service()
    _BROWSER.register(service)
    _BROWSER.bind_owner(service)
    try:
        read_only = replace(session, scopes=frozenset({"kg:read"}))
        with use_session(read_only), pytest.raises(PermissionError):
            await dispatch_browser_control(
                _BROWSER,
                "issue_lease",
                {
                    "document_ref": "document_" + "a" * 64,
                    "tool_ids": ["overstory.get-page-context"],
                    "attended": True,
                },
            )
    finally:
        _BROWSER.unregister(service)


def test_both_production_webui_compositions_inject_the_same_authority() -> None:
    from arbora.webui_host import webui_co_service

    source = Path(webui_co_service.__file__).read_text(encoding="utf-8")
    assert "browser_control_factory_kwargs(" in source
    assert "**browser_control_kwargs" in source
    assert "browser.register(" in source
    assert "browser.unregister(" in source


@pytest.mark.asyncio
async def test_discovery_reports_unavailable_without_granting_a_session(session):
    from types import SimpleNamespace

    from arbora.browser_control.browser_control_runtime import (
        dispatch_browser_operation,
    )

    with pytest.raises(PermissionError):
        await dispatch_browser_control(_BROWSER, "discover", {})
    with use_session(session):
        receipt = await dispatch_browser_operation(
            _BROWSER, "discover", {}, SimpleNamespace(session=session)
        )
        assert receipt.bridge_available is False
        assert receipt.documents == ()
        with pytest.raises(ValueError):
            await dispatch_browser_control(_BROWSER, "discover", {"tenant": "other"})


@pytest.mark.asyncio
async def test_discovery_tracks_binding_and_teardown(session):
    from arbora.api.domain.browser_control.browser_control_api import (
        BrowserDiscoveryReceipt,
    )

    class DiscoveryService:
        async def discover(self, request):
            assert current_session() == session
            return BrowserDiscoveryReceipt(bridge_available=True)

    service = DiscoveryService()
    _BROWSER.register(service)
    try:
        _BROWSER.bind_owner(service)
        with use_session(session):
            receipt = await dispatch_browser_control(_BROWSER, "discover", {})
            assert receipt.bridge_available is True
            assert receipt.documents == ()
    finally:
        _BROWSER.unregister(service)
    with use_session(session):
        assert (
            await dispatch_browser_control(_BROWSER, "discover", {})
        ).bridge_available is False


@pytest.mark.asyncio
async def test_webui_start_binds_bridge_before_first_browser_and_clears_on_stop(
    session,
):
    from arbora.api.domain.browser_control.browser_control_api import (
        BrowserDiscoveryReceipt,
    )
    from arbora.webui_host.webui_co_service import _serve_until_stopped

    stop = threading.Event()
    seen = []

    class Service:
        async def discover(self, request):
            return BrowserDiscoveryReceipt(bridge_available=True)

    class Server:
        should_exit = False

        async def serve(self):
            with use_session(session):
                seen.append(await dispatch_browser_control(_BROWSER, "discover", {}))
            stop.set()

    await _serve_until_stopped(
        Server(),
        stop,
        Service(),
        _BROWSER,
    )
    assert seen[0].bridge_available is True
    assert seen[0].documents == ()
    with use_session(session):
        assert (
            await dispatch_browser_control(_BROWSER, "discover", {})
        ).bridge_available is False
