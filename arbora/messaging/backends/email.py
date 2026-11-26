"""Email channel adapter (ARBORA-MESSAGING-R009.4).

IMAP mailbox for intake and SMTP for delivery. Threads use Message-ID and
References. Inbound is polled every ``MESSAGING_EMAIL_POLL_INTERVAL_S`` seconds
(default 30, range [5, 600]) and yields only messages whose IMAP UID is above
the cursor. The mailbox password is ``setting('MESSAGING_EMAIL_TOKEN')``; hosts
and the account come from ``MESSAGING_EMAIL_IMAP_HOST``,
``MESSAGING_EMAIL_SMTP_HOST`` and ``MESSAGING_EMAIL_USER``. The standard
library mail clients are imported inside ``connect``.
"""

from __future__ import annotations

import asyncio
import email as email_lib
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from email.message import EmailMessage
from typing import Any

from arbora.core.settings import setting
from arbora.messaging.backends._channel_http import (
    POLL_INTERVAL_DEFAULT_S,
    POLL_INTERVAL_RANGE_S,
    clamp,
)
from arbora.messaging.base import MessagingBackend
from arbora.messaging.capabilities import CAPABILITY_MATRIX, MessagingCapabilities
from arbora.messaging.models import (
    EventType,
    InboundEvent,
    Message,
    MessageDirection,
    MessagingConfig,
    PlatformId,
    SendResult,
)
from arbora.messaging.supervision import AdapterImportFailed

logger = logging.getLogger(__name__)


class EmailBackend(MessagingBackend):
    """IMAP intake and SMTP delivery."""

    def __init__(
        self,
        config: MessagingConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        super().__init__(config)
        self._imaplib: Any = None
        self._smtplib: Any = None
        self._password: str | None = None
        self._sleep = sleep
        self._cursor = 0

    @property
    def id(self) -> str:
        return "email"

    @property
    def capabilities(self) -> MessagingCapabilities:
        return CAPABILITY_MATRIX["email"]

    def _setting(self, suffix: str, default: str = "") -> str:
        return str(setting(f"MESSAGING_EMAIL_{suffix}", default) or default)

    async def connect(self) -> None:
        try:
            import imaplib
            import smtplib
        except ImportError as exc:
            raise AdapterImportFailed(
                "Install: pip install arbora[messaging-email]"
            ) from exc
        self._password = self.config.token or self._setting("TOKEN") or None
        if not (
            self._password and self._setting("USER") and self._setting("SMTP_HOST")
        ):
            raise ValueError("Set MESSAGING_EMAIL_TOKEN, _USER and _SMTP_HOST.")
        self._imaplib, self._smtplib = imaplib, smtplib
        self._connected = True

    def _build(
        self, to: str, text: str, reply_to_id: str, thread_id: str
    ) -> EmailMessage:
        message = EmailMessage()
        message["From"] = self._setting("USER")
        message["To"] = to
        message["Subject"] = "Re: Arbora" if reply_to_id else "Arbora"
        message["Message-ID"] = f"<{uuid.uuid4().hex}@arbora>"
        if reply_to_id:
            message["In-Reply-To"] = reply_to_id
            message["References"] = " ".join(x for x in (thread_id, reply_to_id) if x)
        message.set_content(text)
        return message

    def _smtp_send(self, message: EmailMessage) -> None:
        with self._smtplib.SMTP_SSL(self._setting("SMTP_HOST")) as smtp:
            smtp.login(self._setting("USER"), self._password or "")
            smtp.send_message(message)

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: str = "",
        reply_to_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        message = self._build(channel_id, text, reply_to_id, thread_id)
        try:
            await asyncio.to_thread(self._smtp_send, message)
        except Exception as exc:  # the platform call failed: report, never raise
            return SendResult(
                success=False,
                platform=PlatformId.EMAIL,
                channel_id=channel_id,
                error=f"SEND_FAILED:{type(exc).__name__}",
            )
        return SendResult(
            success=True,
            platform=PlatformId.EMAIL,
            channel_id=channel_id,
            message_id=str(message["Message-ID"]),
        )

    def _fetch_after(self, cursor: int) -> list[tuple[int, bytes]]:
        box = self._imaplib.IMAP4_SSL(self._setting("IMAP_HOST"))
        try:
            box.login(self._setting("USER"), self._password or "")
            box.select("INBOX")
            _, data = box.uid("search", None, f"UID {cursor + 1}:*")
            found: list[tuple[int, bytes]] = []
            for raw_uid in (data[0] or b"").split():
                uid = int(raw_uid)
                if uid <= cursor:
                    continue
                _, parts = box.uid("fetch", raw_uid, "(RFC822)")
                found.append((uid, parts[0][1]))
            return found
        finally:
            box.logout()

    def _event(self, raw: bytes) -> InboundEvent | None:
        parsed = email_lib.message_from_bytes(raw)
        body = (
            parsed.get_body(preferencelist=("plain",))
            if hasattr(parsed, "get_body")
            else None
        )
        text = body.get_content() if body is not None else ""
        sender = email_lib.utils.parseaddr(parsed.get("From", ""))[1]
        if not text.strip() or not sender:
            return None
        ident = str(parsed.get("Message-ID", ""))
        return InboundEvent(
            event_type=EventType.MESSAGE,
            platform=PlatformId.EMAIL,
            channel_id=sender,
            thread_id=str(parsed.get("References", "")).split(" ")[0],
            user_id=sender,
            content=text,
            message=Message(
                id=ident,
                content=text,
                platform=PlatformId.EMAIL,
                direction=MessageDirection.INBOUND,
            ),
        )

    async def listen(self) -> AsyncIterator[InboundEvent]:
        interval = clamp(
            float(self._setting("POLL_INTERVAL_S", str(POLL_INTERVAL_DEFAULT_S))),
            POLL_INTERVAL_RANGE_S,
        )
        while self._connected:
            for uid, raw in await asyncio.to_thread(self._fetch_after, self._cursor):
                self._cursor = max(self._cursor, uid)
                event = self._event(raw)
                if event is not None:
                    yield event
            await self._sleep(interval)
