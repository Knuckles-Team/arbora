"""Dependency-independent refusals and bounded pending admission."""

import asyncio

import pytest

from arbora.access.action_policy import ActionPolicy
from arbora.api.contracts.session import use_session
from arbora.identity.local_browser_channel import LocalBrowserChannelOwner
from arbora.mcp_server.served_engine import ServedEngine
from arbora.security import anonymous
from tests.browser_control.test_local_browser_channel import _socket
from tests.browser_control.test_local_browser_transport import _transport
from tests.security.test_anonymous_mode import local_mode as local_mode

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def _owner():
    # Refusals must occur before any native client is requested.
    engine = ServedEngine(None)
    return LocalBrowserChannelOwner(engine, ActionPolicy(engine), _transport())


def test_private_socket_membership_cannot_come_from_scope_json(local_mode):
    owner = _owner()
    scope = _socket()
    scope["state"] = {"admitted": True, "lease": {"status": "active"}}
    with pytest.raises(PermissionError):
        asyncio.run(owner.validate(scope))


@pytest.mark.parametrize("kind", ["http", "lifespan", None])
def test_non_websocket_owner_entry_refuses_without_native_authority(local_mode, kind):
    owner, scope, session = _owner(), _socket(), anonymous.session()
    scope["type"] = kind

    async def run():
        with use_session(session), pytest.raises(PermissionError):
            async with owner.channel(scope, session, ()):
                raise AssertionError("non-socket must not enter")
        assert not owner._pending and not owner._sockets

    asyncio.run(run())


def test_pending_admissions_are_bounded_and_cancel_cleanly(local_mode, monkeypatch):
    owner, session = _owner(), anonymous.session()

    async def run():
        blocked = asyncio.Event()

        async def pending_policy():
            await blocked.wait()
            raise AssertionError("cancelled requests must not reach native storage")

        monkeypatch.setattr(owner, "_policy_revision", pending_policy)

        async def enter():
            async with owner.channel(_socket(), session, ()):
                raise AssertionError("no admission policy result exists")

        with use_session(session):
            tasks = [asyncio.create_task(enter()) for _ in range(16)]
            for _ in range(20):
                if len(owner._pending) == 16:
                    break
                await asyncio.sleep(0)
            assert len(owner._pending) == 16
            with pytest.raises(PermissionError, match="capacity"):
                await enter()
            for task in tasks:
                task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert not owner._pending and not owner._sockets

    asyncio.run(run())
