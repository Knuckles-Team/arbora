"""The public channel adapters are supplied by Arbora."""

from __future__ import annotations

import asyncio
import json
import sys
from importlib import import_module
from typing import Any

import pytest

from arbora.messaging.backends._channel_http import sign_body
from arbora.messaging.base import InboundSignatureInvalid, MessagingBackend
from arbora.messaging.models import EventType, InboundEvent, MessagingConfig
from arbora.messaging.router import InboundRouter


@pytest.mark.parametrize(
    ("platform", "class_name"),
    [
        ("mattermost", "MattermostBackend"),
        ("telegram", "TelegramBackend"),
    ],
)
def test_arbora_owns_channel_adapter(platform: str, class_name: str) -> None:
    adapter = getattr(
        import_module(f"arbora.messaging.backends.{platform}"), class_name
    )

    assert issubclass(adapter, MessagingBackend)
    assert adapter.__module__ == f"arbora.messaging.backends.{platform}"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R800.3")
@pytest.mark.parametrize(
    ("platform", "class_name", "sdk_module", "extra"),
    [
        ("mattermost", "MattermostBackend", "mattermostdriver", "messaging-mattermost"),
        ("telegram", "TelegramBackend", "telegram.ext", "messaging-telegram"),
    ],
)
async def test_connect_names_the_extra_when_the_sdk_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    platform: str,
    class_name: str,
    sdk_module: str,
    extra: str,
) -> None:
    """arbora.messaging imports cleanly without any platform SDK installed.

    Each backend's own ``connect()`` defers its SDK import so the module itself
    is always importable; only calling ``connect()`` without that one platform's
    SDK installed should fail, with a message naming the extra to install.
    """
    adapter = getattr(
        import_module(f"arbora.messaging.backends.{platform}"), class_name
    )
    # A None entry in sys.modules makes the next `import <sdk_module>` raise
    # ImportError, simulating the SDK not being installed, without uninstalling it.
    monkeypatch.setitem(sys.modules, sdk_module, None)

    with pytest.raises(ImportError, match=extra):
        await adapter().connect()


_SAMPLES: dict[str, dict[str, Any]] = {
    "bluebubbles": json.loads(
        '{"type": "new-message", "data": {"guid": "g1", "text": "hi", "chats": [{"guid": "c1"}], "handle": {"address": "+1555"}}}'
    ),
    "buzz": json.loads('{"id": "e1", "room": "r1", "pubkey": "pk", "content": "hi"}'),
    "dingtalk": json.loads(
        '{"msgId": "m1", "conversationId": "cv", "senderStaffId": "u", "text": {"content": "hi"}}'
    ),
    "feishu": json.loads(
        '{"event": {"message": {"chat_id": "oc1", "message_id": "m1", "content": "{\\"text\\": \\"hi\\"}"}, "sender": {"sender_id": {"open_id": "ou1"}}}}'
    ),
    "nostr": json.loads('{"id": "e", "pubkey": "pk", "content": "hi"}'),
    "ntfy": json.loads(
        '{"id": "n1", "event": "message", "topic": "t", "message": "hi"}'
    ),
    "photon": json.loads('{"id": "1", "chat": "c", "sender": "s", "text": "hi"}'),
    "qqbot": json.loads(
        '{"d": {"id": "1", "channel_id": "c", "author": {"id": "a"}, "content": "hi"}}'
    ),
    "raft": json.loads('{"id": "1", "channel": "c", "author": "a", "text": "hi"}'),
    "reef": json.loads('{"id": "1", "conversation": "c", "peer": "p", "text": "hi"}'),
    "simplex": json.loads(
        '{"resp": {"chatItem": {"chatInfo": {"contact": {"localDisplayName": "bob"}}, "chatItem": {"content": {"msgContent": {"text": "hi"}}, "meta": {"itemId": 7}}}}}'
    ),
    "sms": json.loads('{"id": "1", "from": "+1555", "body": "hi"}'),
    "tlon": json.loads('{"id": "1", "nest": "chat/x", "author": "~zod", "text": "hi"}'),
    "webhook": json.loads(
        '{"id": "1", "channel_id": "c", "sender": "s", "text": "hi"}'
    ),
    "wecom": json.loads('{"MsgId": 1, "FromUserName": "u", "Content": "hi"}'),
    "weixin": json.loads('{"id": "1", "from": "u", "text": "hi"}'),
    "x": json.loads('{"id": "100", "author_id": "a", "text": "hi"}'),
    "yuanbao": json.loads(
        '{"id": "1", "conversation": "c", "user": "u", "text": "hi"}'
    ),
    "zalo": json.loads(
        '{"message": {"msg_id": "1", "text": "hi"}, "sender": {"id": "u"}}'
    ),
    "zalouser": json.loads('{"id": "1", "thread": "t", "from": "u", "text": "hi"}'),
}
_CLASSES = {
    "bluebubbles": "BlueBubblesBackend",
    "buzz": "BuzzBackend",
    "dingtalk": "DingTalkBackend",
    "email": "EmailBackend",
    "feishu": "FeishuBackend",
    "nostr": "NostrBackend",
    "ntfy": "NtfyBackend",
    "photon": "PhotonBackend",
    "qqbot": "QQBotBackend",
    "raft": "RaftBackend",
    "reef": "ReefBackend",
    "simplex": "SimplexBackend",
    "sms": "SmsBackend",
    "tlon": "TlonBackend",
    "webhook": "WebhookBackend",
    "wecom": "WeComBackend",
    "weixin": "WeixinBackend",
    "x": "XBackend",
    "yuanbao": "YuanbaoBackend",
    "zalo": "ZaloBackend",
    "zalouser": "ZaloUserBackend",
}


def _backend(platform: str) -> MessagingBackend:
    module = import_module(f"arbora.messaging.backends.{platform}")
    backend = getattr(module, _CLASSES[platform])(MessagingConfig(platform=platform))
    backend._connected = True
    return backend


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)  # a controlled clock: yield, never wait


async def _through_router(backend: MessagingBackend) -> InboundEvent:
    """Run the real router over ``backend`` and return the first routed event."""
    seen: asyncio.Future[InboundEvent] = asyncio.get_running_loop().create_future()

    async def handler(event: InboundEvent, source: MessagingBackend) -> None:
        if not seen.done():
            seen.set_result(event)

    router = InboundRouter()
    router.register_backend(backend)
    router.set_default_handler(handler)
    task = asyncio.create_task(router.start())
    try:
        return await asyncio.wait_for(seen, timeout=5)
    finally:
        backend._connected = False
        await router.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def _inbound(platform: str, monkeypatch: pytest.MonkeyPatch) -> None:
    backend: Any = _backend(platform)
    sample = _SAMPLES.get(platform)
    if platform == "email":
        backend._fetch_after = lambda cursor: [
            (5, b"From: a@example.org\r\nMessage-ID: <m1@x>\r\n\r\nhi\r\n")
        ]
        backend._sleep = _no_sleep
    elif backend.inbound_mode == "webhook":
        monkeypatch.setattr(
            "arbora.messaging.backends._channel_http.resolve_secret",
            lambda ref: "s3cret",
        )
        body = json.dumps(sample).encode()
        with pytest.raises(InboundSignatureInvalid):
            backend.verify_inbound({"X-Arbora-Signature": "sha256=00"}, body)
        event = backend.verify_inbound(
            {"X-Arbora-Signature": sign_body("s3cret", body)}, body
        )
        await backend.enqueue_inbound(event)
    elif backend.inbound_mode == "poll":

        async def poll_once() -> list[dict[str, Any]]:
            return [sample]

        backend._poll_once = poll_once
        backend._sleep = _no_sleep
    else:

        async def open_stream():
            yield sample

        backend._open_stream = open_stream
        backend._sleep = _no_sleep
    event = await _through_router(backend)
    assert event.platform == platform
    assert event.event_type == EventType.MESSAGE
    assert event.content == "hi"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.1")
async def test_bluebubbles_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("bluebubbles", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.2")
async def test_buzz_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("buzz", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.3")
async def test_dingtalk_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("dingtalk", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.4")
async def test_email_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("email", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.5")
async def test_feishu_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("feishu", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.6")
async def test_nostr_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("nostr", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.7")
async def test_ntfy_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("ntfy", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.8")
async def test_photon_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("photon", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.9")
async def test_qqbot_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("qqbot", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.10")
async def test_raft_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("raft", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.11")
async def test_reef_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("reef", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.12")
async def test_simplex_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("simplex", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.13")
async def test_sms_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("sms", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.14")
async def test_tlon_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("tlon", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.15")
async def test_webhook_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("webhook", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.16")
async def test_wecom_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("wecom", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.17")
async def test_weixin_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("weixin", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.18")
async def test_x_inbound_event_reaches_router(monkeypatch: pytest.MonkeyPatch) -> None:
    await _inbound("x", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.19")
async def test_yuanbao_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("yuanbao", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.20")
async def test_zalo_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("zalo", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.21")
async def test_zalouser_inbound_event_reaches_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _inbound("zalouser", monkeypatch)


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.18")
async def test_x_poll_yields_only_items_newer_than_the_cursor() -> None:
    backend: Any = _backend("x")
    backend._cursor = "100"

    async def poll_once() -> list[dict[str, Any]]:
        return [
            {"id": "99", "author_id": "a", "text": "old"},
            {"id": "101", "author_id": "a", "text": "new"},
        ]

    async def stop(_: float) -> None:
        backend._connected = False

    backend._poll_once = poll_once
    backend._sleep = stop
    texts = [event.content async for event in backend.listen()]
    assert texts == ["new"]
    assert backend._cursor == "101"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-MESSAGING-R009.12")
async def test_stream_adapter_reconnects_with_bounded_backoff() -> None:
    backend: Any = _backend("simplex")
    delays: list[float] = []
    opened = {"n": 0}

    async def open_stream():
        opened["n"] += 1
        if opened["n"] < 4:
            raise OSError("closed")
        yield _SAMPLES["simplex"]

    async def sleep(delay: float) -> None:
        delays.append(delay)

    backend._open_stream = open_stream
    backend._sleep = sleep
    first = await anext(backend.listen())
    assert first.content == "hi"
    assert delays == [1.0, 2.0, 4.0]
