"""Shared HTTP channel adapter base (ARBORA-MESSAGING-R009).

Every additional channel platform of ``ARBORA-MESSAGING-R009`` is a thin
subclass of :class:`HttpChannelBackend`: it names its platform, its send
endpoint, how a request body is built and how one raw inbound payload maps to
an ``InboundEvent``. The base owns the three inbound shapes the spec names:

* ``webhook``: ``verify_inbound`` checks an HMAC-SHA256 signature over the raw
  body, the shared ingress queues the event, ``listen`` drains the queue.
* ``stream``: ``listen`` holds one long-lived outbound connection and reconnects
  with a bounded exponential backoff (``ARBORA-MESSAGING-R003``).
* ``poll``: ``listen`` polls on ``MESSAGING_<ID>_POLL_INTERVAL_S`` and yields
  only items newer than the cursor. A restart replays at most one poll window;
  ``arbora/messaging/inbox.py`` derives its row id from the platform message
  id, so a replayed item never opens a second turn.

Credentials come from ``setting('MESSAGING_<ID>_TOKEN')`` (the secrets backend
resolves it). The webhook signing secret is the secrets reference in
``MESSAGING_<ID>_WEBHOOK_SECRET_REF``. The HTTP client library is imported
inside ``connect`` and its absence raises ``AdapterImportFailed``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, ClassVar

from arbora.core.settings import setting
from arbora.messaging.base import InboundSignatureInvalid, MessagingBackend
from arbora.messaging.capabilities import CAPABILITY_MATRIX, MessagingCapabilities
from arbora.messaging.models import (
    EventType,
    InboundEvent,
    Message,
    MessageDirection,
    MessagingConfig,
    SendResult,
)
from arbora.messaging.supervision import AdapterImportFailed
from arbora.messaging.transport import channel_http_client, resolve_secret

logger = logging.getLogger(__name__)

POLL_INTERVAL_DEFAULT_S = 30.0
POLL_INTERVAL_RANGE_S = (5.0, 600.0)
RECONNECT_BASE_S = 1.0
RECONNECT_CAP_S = 60.0
SIGNATURE_HEADER = "x-arbora-signature"


def clamp(value: float, bounds: tuple[float, float]) -> float:
    """Return ``value`` limited to the closed range ``bounds``."""
    return max(bounds[0], min(bounds[1], value))


def sign_body(secret: str, body: bytes) -> str:
    """Return the ``sha256=<hex>`` signature of ``body`` under ``secret``."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class HttpChannelBackend(MessagingBackend):
    """A channel adapter over one HTTP gateway (ARBORA-MESSAGING-R009)."""

    platform: ClassVar[str] = ""
    inbound_mode: ClassVar[str] = "webhook"  # webhook | stream | poll
    default_url: ClassVar[str] = ""
    send_path: ClassVar[str] = "/messages"
    reply_path: ClassVar[str] = ""  # formatted with ``message_id`` when set
    stream_path: ClassVar[str] = "/events"
    poll_path: ClassVar[str] = "/messages"
    auth_param: ClassVar[str] = ""  # query parameter instead of a header

    def __init__(
        self,
        config: MessagingConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(config)
        self._client: Any = None
        self._token: str | None = None
        self._sleep = sleep
        self._event_queue: asyncio.Queue[InboundEvent] = asyncio.Queue()
        self._cursor = ""

    # ── identity ────────────────────────────────────────────────────

    @property
    def id(self) -> str:
        return self.platform

    @property
    def capabilities(self) -> MessagingCapabilities:
        return CAPABILITY_MATRIX[self.platform]

    def _key(self, suffix: str) -> str:
        return f"MESSAGING_{self.platform.upper()}_{suffix}"

    def _base_url(self) -> str:
        url = self.config.webhook_url or str(
            setting(self._key("URL"), self.default_url) or ""
        )
        return url.rstrip("/")

    # ── lifecycle ───────────────────────────────────────────────────

    async def connect(self) -> None:
        try:
            import httpx  # noqa: F401  (the adapter's client library)
        except ImportError as exc:
            raise AdapterImportFailed(
                f"Install: pip install arbora[messaging-{self.platform}]"
            ) from exc
        self._token = (
            self.config.token or str(setting(self._key("TOKEN"), "") or "") or None
        )
        if not self._token:
            raise ValueError(f"Set {self._key('TOKEN')}.")
        if not self._base_url():
            raise ValueError(f"Set {self._key('URL')}.")
        self._client = channel_http_client(self.platform, timeout=30.0)
        self._connected = True

    async def disconnect(self) -> None:
        self._connected = False
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # ── outbound ────────────────────────────────────────────────────

    def auth(self) -> tuple[dict[str, str], dict[str, str]]:
        """Return the (headers, query params) that carry the credential."""
        if self.auth_param:
            return {}, {self.auth_param: str(self._token)}
        return {"Authorization": f"Bearer {self._token}"}, {}

    def send_body(
        self, channel_id: str, text: str, *, thread_id: str, reply_to_id: str
    ) -> dict[str, Any]:
        """Build the platform request body for one text message."""
        body: dict[str, Any] = {"to": channel_id, "text": text}
        if reply_to_id:
            body["reply_to"] = reply_to_id
        if thread_id:
            body["thread"] = thread_id
        return body

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: str = "",
        reply_to_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        try:
            headers, params = self.auth()
            path = (
                self.reply_path.replace("{message_id}", reply_to_id)
                if reply_to_id and self.reply_path
                else self.send_path
            ).replace("{channel_id}", channel_id)
            response = await self._client.post(
                self._base_url() + path,
                json=self.send_body(
                    channel_id, text, thread_id=thread_id, reply_to_id=reply_to_id
                ),
                headers=headers,
                params=params,
            )
            status = int(response.status_code)
            if not 200 <= status < 300:
                return SendResult(
                    success=False,
                    platform=self.platform,
                    channel_id=channel_id,
                    error=f"SEND_FAILED:{status}",
                )
            message_id = ""
            try:
                message_id = str((response.json() or {}).get("id", ""))
            except Exception:  # a non-JSON answer still means the send was accepted
                message_id = ""
            return SendResult(
                success=True,
                platform=self.platform,
                channel_id=channel_id,
                message_id=message_id,
            )
        except Exception as exc:  # the platform call failed: report, never raise
            return SendResult(
                success=False,
                platform=self.platform,
                channel_id=channel_id,
                error=f"SEND_FAILED:{type(exc).__name__}",
            )

    # ── inbound ─────────────────────────────────────────────────────

    def fields(self, raw: Mapping[str, Any]) -> Mapping[str, Any] | None:
        """Map one raw payload to channel, user, text, id and thread, or None."""
        return {
            "channel_id": raw.get("channel_id") or raw.get("chat_id") or "",
            "user_id": raw.get("user_id") or raw.get("from") or "",
            "text": raw.get("text") or raw.get("message") or "",
            "id": raw.get("id") or "",
            "thread_id": raw.get("thread_id") or "",
        }

    def parse_event(self, raw: Mapping[str, Any]) -> InboundEvent | None:
        mapped = self.fields(raw)
        if not mapped or not str(mapped.get("text", "")).strip():
            return None
        text = str(mapped["text"])
        channel = str(mapped["channel_id"] or mapped["user_id"])
        return InboundEvent(
            event_type=EventType.MESSAGE,
            platform=self.platform,
            channel_id=channel,
            thread_id=str(mapped.get("thread_id", "")),
            user_id=str(mapped["user_id"]),
            content=text,
            message=Message(
                id=str(mapped.get("id", "")),
                content=text,
                platform=self.platform,
                direction=MessageDirection.INBOUND,
            ),
            raw=dict(raw),
        )

    def verify_inbound(self, headers: Mapping[str, str], body: bytes) -> InboundEvent:
        if self.inbound_mode != "webhook":
            return super().verify_inbound(headers, body)
        secret = resolve_secret(str(setting(self._key("WEBHOOK_SECRET_REF"), "") or ""))
        if not secret:
            raise InboundSignatureInvalid("no signing secret is configured")
        given = {k.lower(): v for k, v in headers.items()}.get(SIGNATURE_HEADER, "")
        if not hmac.compare_digest(given, sign_body(secret, body)):
            raise InboundSignatureInvalid("signature mismatch")
        try:
            raw = json.loads(body)
        except ValueError as exc:
            raise InboundSignatureInvalid("body is not JSON") from exc
        event = self.parse_event(raw) if isinstance(raw, dict) else None
        if event is None:
            raise InboundSignatureInvalid("no message in the payload")
        return event

    async def enqueue_inbound(self, event: InboundEvent) -> None:
        if self.inbound_mode != "webhook":
            await super().enqueue_inbound(event)
        await self._event_queue.put(event)

    async def _open_stream(self) -> AsyncIterator[Mapping[str, Any]]:
        headers, params = self.auth()
        url = str(setting(self._key("STREAM_URL"), "") or "") or (
            self._base_url() + self.stream_path
        )
        async with self._client.stream(
            "GET", url, headers=headers, params=params
        ) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if line.strip():
                    item = json.loads(line)
                    if isinstance(item, dict):
                        yield item

    async def _poll_once(self) -> list[Mapping[str, Any]]:
        headers, params = self.auth()
        if self._cursor:
            params = {**params, "since": self._cursor}
        response = await self._client.get(
            self._base_url() + self.poll_path, headers=headers, params=params
        )
        response.raise_for_status()
        items = response.json()
        items = items.get("items", []) if isinstance(items, dict) else items
        return [item for item in items if isinstance(item, dict)]

    def _newer(self, item: Mapping[str, Any]) -> bool:
        ident = str(item.get("id", ""))
        return (
            not self._cursor
            or (
                ident.isdigit()
                and self._cursor.isdigit()
                and int(ident) > int(self._cursor)
            )
            or (not ident.isdigit() and ident > self._cursor)
        )

    async def listen(self) -> AsyncIterator[InboundEvent]:
        if self.inbound_mode == "webhook":
            while self._connected:
                try:
                    yield await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                except TimeoutError:
                    continue
            return
        if self.inbound_mode == "poll":
            interval = clamp(
                float(
                    setting(self._key("POLL_INTERVAL_S"), POLL_INTERVAL_DEFAULT_S)
                    or POLL_INTERVAL_DEFAULT_S
                ),
                POLL_INTERVAL_RANGE_S,
            )
            while self._connected:
                for item in await self._poll_once():
                    if not self._newer(item):
                        continue
                    self._cursor = str(item.get("id", self._cursor))
                    event = self.parse_event(item)
                    if event is not None:
                        yield event
                await self._sleep(interval)
            return
        attempt = 0
        while self._connected:
            try:
                async for item in self._open_stream():
                    attempt = 0
                    event = self.parse_event(item)
                    if event is not None:
                        yield event
            except (OSError, ValueError) as exc:
                logger.warning(
                    "%s stream ended (%s)", self.platform, type(exc).__name__
                )
            except (
                Exception
            ) as exc:  # an HTTP transport error is a reconnect, not a crash
                logger.warning(
                    "%s stream error (%s)", self.platform, type(exc).__name__
                )
            await self._sleep(min(RECONNECT_CAP_S, RECONNECT_BASE_S * 2**attempt))
            attempt += 1
