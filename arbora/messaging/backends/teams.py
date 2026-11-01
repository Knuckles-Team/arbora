"""Microsoft Teams Messaging Backend (CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction).

Implements ``MessagingBackend`` for Microsoft Teams using ``botbuilder-core``.
Supports Adaptive Cards, threads, reactions, and Bot Framework webhook events.

Install::

    pip install agent-utilities[messaging-teams]

Configuration::

    MSTEAMS_APP_ID=<app-id>
    MSTEAMS_APP_PASSWORD=<app-password>

CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction — Native Messaging Backend Abstraction
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from arbora.messaging.base import MessagingBackend
from arbora.messaging.capabilities import (
    CAPABILITY_MATRIX,
    MessagingCapabilities,
)
from arbora.messaging.models import (
    EventType,
    InboundEvent,
    Message,
    MessageDirection,
    MessagingConfig,
    PlatformId,
    SendResult,
    Thread,
)

logger = logging.getLogger(__name__)

#: The Bot Framework login endpoint for a bot's client credentials.
_BOT_FRAMEWORK_LOGIN = (
    "https://login.microsoftonline.com/botframework.com/oauth2/v2.0/token"
)


class TeamsBackend(MessagingBackend):
    """Microsoft Teams messaging backend using ``botbuilder-core``. CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""

    def __init__(self, config: MessagingConfig | None = None) -> None:
        super().__init__(config)
        self._adapter: Any = None
        self._event_queue: asyncio.Queue[InboundEvent] = asyncio.Queue()

    @property
    def id(self) -> str:
        return "teams"

    @property
    def capabilities(self) -> MessagingCapabilities:
        return CAPABILITY_MATRIX["teams"]

    async def connect(self) -> None:
        """Initialize Bot Framework adapter. CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""
        try:
            from botbuilder.core import BotFrameworkAdapter, BotFrameworkAdapterSettings
        except ImportError:
            raise ImportError(
                "botbuilder-core is required. Install: pip install agent-utilities[messaging-teams]"
            ) from None

        if not self.config.app_id or not self.config.app_secret:
            raise ValueError("Set MSTEAMS_APP_ID and MSTEAMS_APP_PASSWORD.")

        settings = BotFrameworkAdapterSettings(
            self.config.app_id, self.config.app_secret
        )
        self._adapter = BotFrameworkAdapter(settings)
        self._connected = True
        logger.info(
            "[CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction] Teams backend connected."
        )

    async def disconnect(self) -> None:
        """Disconnect from Teams. CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""
        await super().disconnect()

    async def send_message(
        self,
        channel_id: str,
        text: str,
        *,
        thread_id: str = "",
        reply_to_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        """Send a Teams message through the Bot Framework Connector REST API.

        ``channel_id`` is the conversation id. ``MSTEAMS_SERVICE_URL`` (or the
        ``service_url`` config extra) names the Connector service of the tenant.
        Adaptive Cards ride in ``metadata["adaptive_card"]``.
        CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction
        """
        from arbora.core.settings import setting
        from arbora.messaging.transport import channel_http_client

        service_url = str(
            self.config.extra.get("service_url") or setting("MSTEAMS_SERVICE_URL", "")
        ).rstrip("/")
        if not service_url or not self.config.app_id or not self.config.app_secret:
            return SendResult(
                success=False,
                platform=PlatformId.TEAMS,
                channel_id=channel_id,
                error="Teams send needs MSTEAMS_SERVICE_URL and app credentials.",
            )
        activity: dict[str, Any] = {"type": "message", "text": text}
        if reply_to_id:
            activity["replyToId"] = reply_to_id
        if metadata and "adaptive_card" in metadata:
            activity["attachments"] = [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": metadata["adaptive_card"],
                }
            ]
        try:
            async with channel_http_client("teams", timeout=30) as client:
                token = await client.post(
                    _BOT_FRAMEWORK_LOGIN,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.config.app_id,
                        "client_secret": self.config.app_secret,
                        "scope": "https://api.botframework.com/.default",
                    },
                )
                token.raise_for_status()
                sent = await client.post(
                    f"{service_url}/v3/conversations/{channel_id}/activities",
                    json=activity,
                    headers={"Authorization": f"Bearer {token.json()['access_token']}"},
                )
                sent.raise_for_status()
            return SendResult(
                success=True,
                message_id=str(sent.json().get("id", "")),
                platform=PlatformId.TEAMS,
                channel_id=channel_id,
            )
        except Exception as e:
            logger.error(
                "[CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction] Teams send failed: %s",
                type(e).__name__,
            )
            return SendResult(
                success=False, platform=PlatformId.TEAMS, error=type(e).__name__
            )

    async def send_typing(self, channel_id: str) -> None:
        """Send typing indicator. CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""
        return None  # Handled via TurnContext in webhook handler

    async def create_thread(
        self, channel_id: str, message_id: str, title: str = ""
    ) -> Thread:
        """Create a thread reply. CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""
        return Thread(
            id=message_id,
            parent_message_id=message_id,
            channel_id=channel_id,
            title=title,
        )

    async def listen(self) -> AsyncIterator[InboundEvent]:
        """Yield inbound Teams events (populated via webhook handler). CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction"""
        while self._connected:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=1.0)
                yield event
            except TimeoutError:
                continue

    async def process_webhook(
        self, body: dict[str, Any], auth_header: str = ""
    ) -> None:
        """Process an incoming Bot Framework webhook activity.

        CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction

        Call this from your web server's webhook endpoint to inject
        Teams events into the inbound router pipeline.

        Args:
            body: The raw activity JSON from Bot Framework.
            auth_header: The Authorization header for verification.
        """
        if auth_header:
            logger.debug(
                "[CONCEPT:MERISTEM-ECO.messaging.native-backend-abstraction] Webhook received with auth header present."
            )
        activity_type = body.get("type", "")
        if activity_type == "message":
            event = InboundEvent(
                event_type=EventType.MESSAGE,
                platform=PlatformId.TEAMS,
                channel_id=body.get("channelId", ""),
                user_id=body.get("from", {}).get("id", ""),
                user_name=body.get("from", {}).get("name", ""),
                content=body.get("text", ""),
                message=Message(
                    id=body.get("id", ""),
                    content=body.get("text", ""),
                    channel_id=body.get("channelId", ""),
                    author_id=body.get("from", {}).get("id", ""),
                    author_name=body.get("from", {}).get("name", ""),
                    platform=PlatformId.TEAMS,
                    direction=MessageDirection.INBOUND,
                ),
                raw=body,
            )
            await self._event_queue.put(event)
