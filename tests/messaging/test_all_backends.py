"""Every published channel adapter is discoverable and sends through a faked API.

ARBORA-MESSAGING-R800.1 (discovery through the published entry-point group)
and ARBORA-MESSAGING-R008 (the alert intake reaches any default platform).
Each adapter's platform client is replaced by a recording double, so the
test proves the adapter turns ``send_message`` into one platform call that
carries the text, and reports success only when that call answers.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import json
import sys
import types
from typing import Any

import httpx
import pytest

from arbora.messaging.models import MessagingConfig
from arbora.messaging.registry import _ENTRY_POINT_GROUP, MessagingRegistry

PLATFORMS = (
    "discord",
    "googlechat",
    "googlemeet",
    "imessage",
    "irc",
    "line",
    "matrix",
    "mattermost",
    "nextcloud",
    "signal",
    "slack",
    "synology",
    "teams",
    "telegram",
    "twitch",
    "voicecall",
    "whatsapp",
)
#: Platforms that have no text channel; their adapter must refuse a send.
NO_TEXT = frozenset({"googlemeet"})
_ANSWERS = {
    "status_code": 200,
    "ok": True,
    "is_success": True,
    "returncode": 0,
    "message_id": 1,
    "id": "1",
    "ts": "1.0",
    "sid": "SM1",
    "event_id": "$1",
    "name": "spaces/1/messages/1",
}


class _Api:
    """A platform client double: every attribute and call is recorded."""

    def __init__(self, calls: list[tuple[str, tuple, dict]], path: str = "") -> None:
        self._calls = calls
        self._path = path

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        if name in _ANSWERS:
            return _ANSWERS[name]
        return _Api(self._calls, f"{self._path}.{name}")

    def __call__(self, *args: Any, **kwargs: Any) -> _Api:
        self._calls.append((self._path, args, kwargs))
        return _Api(self._calls, f"{self._path}()")

    def __await__(self):
        yield from asyncio.sleep(0).__await__()
        return self

    def __getitem__(self, key: Any) -> Any:
        return _ANSWERS.get(str(key), "1")

    def get(self, key: Any, default: Any = None) -> Any:
        return _ANSWERS.get(str(key), "1")

    def json(self) -> dict[str, Any]:
        return {"ok": True, "id": "1", "message_id": 1, "ocs": {"data": {"id": 1}}}

    def raise_for_status(self) -> None:
        return None

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"", b""


def _fake_sdk(monkeypatch: pytest.MonkeyPatch, calls: list) -> None:
    """Stand in for the SDK classes some adapters build a request with."""

    def module(name: str, **attrs: Any) -> None:
        mod = types.ModuleType(name)
        vars(mod).update(attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    def record(kind: str):
        def build(*args: Any, **kwargs: Any) -> dict[str, Any]:
            calls.append((kind, args, kwargs))
            return {"kind": kind, "args": args, **kwargs}

        return build

    for parent in ("linebot", "linebot.v3"):
        module(parent)
    module(
        "linebot.v3.messaging",
        PushMessageRequest=record("PushMessageRequest"),
        TextMessage=record("TextMessage"),
    )


def _carries(calls: list, text: str) -> bool:
    return any(
        text in json.dumps([args, kwargs], default=str) for _, args, kwargs in calls
    )


def _teams_transport(calls: list) -> httpx.MockTransport:
    def answer(request: httpx.Request) -> httpx.Response:
        calls.append((str(request.url), (request.content.decode(),), {}))
        if "oauth2" in str(request.url):
            return httpx.Response(200, json={"access_token": "t"})
        return httpx.Response(201, json={"id": "activity-1"})

    return httpx.MockTransport(answer)


@pytest.mark.spec("ARBORA-MESSAGING-R800.1")
def test_every_platform_is_published_and_discovered() -> None:
    published = {
        ep.name: ep
        for ep in importlib.metadata.entry_points().select(group=_ENTRY_POINT_GROUP)
    }
    assert set(PLATFORMS) <= set(published)
    registry = MessagingRegistry()
    for platform in PLATFORMS:
        assert registry.is_installed(platform), platform
        backend = published[platform].load()(MessagingConfig(platform=platform))
        assert backend.id == platform


@pytest.mark.spec("ARBORA-MESSAGING-R008")
@pytest.mark.parametrize("platform", PLATFORMS)
async def test_send_reaches_the_faked_platform_api(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, tuple, dict]] = []
    _fake_sdk(monkeypatch, calls)
    ep = next(
        ep
        for ep in importlib.metadata.entry_points().select(group=_ENTRY_POINT_GROUP)
        if ep.name == platform
    )
    config = MessagingConfig(
        platform=platform,
        token="token",
        app_id="app",
        app_secret="secret",
        extra={"service_url": "https://teams.example", "from_number": "+15550000"},
    )
    backend = ep.load()(config)
    api = _Api(calls)
    for attribute in (
        "_client",
        "_app",
        "_driver",
        "_service",
        "_api",
        "_bot",
        "_connection",
    ):
        if hasattr(backend, attribute):
            setattr(backend, attribute, api)
    backend._connected = True
    if platform == "teams":
        transport = _teams_transport(calls)
        monkeypatch.setattr(
            "arbora.messaging.transport.channel_http_client",
            lambda service, **kw: httpx.AsyncClient(transport=transport),
        )
    if platform == "imessage":
        monkeypatch.setattr(
            asyncio,
            "create_subprocess_exec",
            lambda *args, **kwargs: _record_process(calls, args),
        )
    if platform == "nextcloud":
        # Talk's OCS API answers a created chat message with 201.
        monkeypatch.setitem(_ANSWERS, "status_code", 201)

    text = f"hello-{platform}"
    result = await backend.send_message("1", text)

    if platform in NO_TEXT:
        assert result.success is False
        assert not _carries(calls, text)
        return
    assert result.success is True, result.error
    assert _carries(calls, text), calls


async def _record_process(calls: list, args: tuple) -> _Api:
    calls.append(("subprocess", args, {}))
    return _Api(calls, "process")
