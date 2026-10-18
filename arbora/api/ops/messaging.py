"""Alert intake operation (ARBORA-MESSAGING-R008).

``messaging.alert`` takes a monitoring webhook on Arbora's served API. It
accepts the shapes monitoring sources send: ``{"text": ...}`` (and the
``msg``/``message``/``content`` aliases) and an Alertmanager webhook body.
Arbora admits the caller as for any other operation, then delivers the text
to the configured default channel:

* ``telegram`` (and every other installed channel adapter) goes through the
  governed messaging service, so the send scope check and the outbound
  mirror apply;
* ``mattermost`` goes through the fleet to the mattermost connector, with the
  same admission ``fleet.call`` uses. Arbora makes no Mattermost API call.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from arbora.api.registry import AuditClass, Composite, Effect, HttpShape, OpSpec, Verb
from arbora.core.settings import setting
from arbora.messaging.alert_intake import extract_alert_text

logger = logging.getLogger(__name__)

#: The fleet server and tool that post a Mattermost message.
MATTERMOST_SERVER_SETTING = "MESSAGING_MATTERMOST_FLEET_SERVER"
MATTERMOST_TOOL_SETTING = "MESSAGING_MATTERMOST_FLEET_TOOL"
_MAX_TEXT = 4_000
_MAX_ALERTS = 100


class MessagingAlertParams(BaseModel):
    """A monitoring webhook body: plain text or an Alertmanager notification."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    text: str | None = Field(default=None, max_length=_MAX_TEXT)
    msg: str | None = Field(default=None, max_length=_MAX_TEXT)
    message: str | None = Field(default=None, max_length=_MAX_TEXT)
    content: str | None = Field(default=None, max_length=_MAX_TEXT)
    alerts: list[dict[str, Any]] | None = Field(default=None, max_length=_MAX_ALERTS)
    status: str | None = Field(default=None, max_length=64)
    receiver: str | None = Field(default=None, max_length=256)
    version: str | None = Field(default=None, max_length=16)
    group_key: str | None = Field(default=None, alias="groupKey", max_length=1024)
    truncated_alerts: int | None = Field(default=None, alias="truncatedAlerts")
    group_labels: dict[str, str] | None = Field(default=None, alias="groupLabels")
    common_labels: dict[str, str] | None = Field(default=None, alias="commonLabels")
    common_annotations: dict[str, str] | None = Field(
        default=None, alias="commonAnnotations"
    )
    external_url: str | None = Field(default=None, alias="externalURL", max_length=2048)


class MessagingAlertResult(BaseModel):
    """Where the alert went; never the channel credentials."""

    delivered: bool
    platform: str
    channel_id: str
    message_id: str = ""


def _refused(code: str, reason: str) -> Exception:
    from arbora.api.invoke.pipeline import OperationRefused

    return OperationRefused(code, {"reason": reason})


def _alert_text(params: Mapping[str, Any]) -> str:
    body = {key: value for key, value in params.items() if value is not None}
    if not any(
        body.get(key) for key in ("text", "msg", "message", "content", "alerts")
    ):
        raise _refused("INVALID_ARGUMENT", "the alert carries no text")
    return extract_alert_text(body)


async def _post_to_mattermost(context: Any, channel: str, text: str) -> str:
    """Post through the fleet's mattermost connector under the caller's authority."""
    from arbora.api.ops import fleet

    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise _refused("UNAVAILABLE", "fleet gateway is not bound")
    server = str(setting(MATTERMOST_SERVER_SETTING, "mattermost-mcp"))
    tool = str(setting(MATTERMOST_TOOL_SETTING, "mattermost_mcp_posts"))
    arguments = {
        "action": "create_post",
        "params_json": json.dumps({"channel_id": channel, "message": text}),
    }
    call_op = next(op for op in fleet.operations() if op.id == "fleet.call")
    params = {"server": server, "tool": tool, "arguments": arguments}
    caller = context.caller
    decision = await gateway.effect(call_op, params, caller)
    result = await gateway.call(
        caller,
        server,
        tool,
        arguments,
        expected_effect=decision.effect,
        service_identity=context.service_identity,
        owner=context.owner,
        owner_ref=getattr(context, "owner_ref", None),
        fleet_decision=decision,
        registry_digest=context.registry_digest,
    )
    if isinstance(result, Mapping) and (result.get("error") or result.get("isError")):
        raise _refused("UPSTREAM_FAILED", "the mattermost connector refused the post")
    post_id = result.get("id", "") if isinstance(result, Mapping) else ""
    return str(post_id or "")


async def handle_messaging_alert(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Deliver one alert to the configured default channel."""
    del op
    text = _alert_text(params)
    platform = str(setting("MESSAGING_DEFAULT_PLATFORM", "telegram") or "telegram")
    channel = str(setting("MESSAGING_DEFAULT_CHANNEL", "") or "")
    if not channel:
        raise _refused("UNAVAILABLE", "alert destination is not configured")
    if platform == "mattermost":
        message_id = await _post_to_mattermost(context, channel, text)
        return MessagingAlertResult(
            delivered=True, platform=platform, channel_id=channel, message_id=message_id
        ).model_dump()

    from arbora.api.contracts.session import use_session
    from arbora.messaging.service import MessagingService
    from arbora.security.actor_context import use_actor

    session = getattr(context.caller, "session", None)
    if session is None:
        raise _refused("UNAUTHENTICATED", "a verified session is required")
    # The send gate reads the ambient session: bind the admitted caller's.
    with use_actor(session.actor), use_session(session):
        sent = await MessagingService.instance().send(
            platform, channel, text, source="alert-intake", reason="monitoring alert"
        )
    if not sent.success:
        # The adapter and gate report a reason without credentials or content.
        logger.warning("alert delivery to %s failed: %s", platform, sent.error)
        raise _refused("UNAVAILABLE", "alert delivery failed")
    return MessagingAlertResult(
        delivered=True,
        platform=platform,
        channel_id=channel,
        message_id=str(sent.message_id or ""),
    ).model_dump()


def operations() -> tuple[OpSpec, ...]:
    return (
        OpSpec(
            id="messaging.alert",
            verb=Verb.ACT,
            summary="Deliver a monitoring alert to the operator's default channel",
            examples=("send this alert to the operator",),
            params=MessagingAlertParams,
            result=MessagingAlertResult,
            binding=Composite(
                handler="arbora.api.ops.messaging.handle_messaging_alert"
            ),
            scopes=frozenset({"kg:write"}),
            http=HttpShape(method="POST", path="/api/v1/messaging/alert"),
            effect=Effect.WRITE,
            audit=AuditClass.EVENT,
        ),
    )


specs = operations

__all__ = [
    "MessagingAlertParams",
    "MessagingAlertResult",
    "handle_messaging_alert",
    "operations",
    "specs",
]
