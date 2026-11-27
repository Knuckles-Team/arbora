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

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.registry import AuditClass, Composite, Effect, HttpShape, OpSpec, Verb
from arbora.core.environment import read_env
from arbora.fleet.fleet_tool_call import call_fleet_tool
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
    gateway = context.services.get("fleet_gateway")
    if gateway is None:
        raise _refused("UNAVAILABLE", "fleet gateway is not bound")
    server = str(read_env(MATTERMOST_SERVER_SETTING, "mattermost-mcp"))
    tool = str(read_env(MATTERMOST_TOOL_SETTING, "mattermost_mcp_posts"))
    arguments = {
        "action": "create_post",
        "params_json": json.dumps({"channel_id": channel, "message": text}),
    }
    result = await call_fleet_tool(context, server, tool, arguments)
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
    platform = str(read_env("MESSAGING_DEFAULT_PLATFORM") or "telegram")
    channel = str(read_env("MESSAGING_DEFAULT_CHANNEL") or "")
    if not channel:
        raise _refused("UNAVAILABLE", "alert destination is not configured")
    if platform == "mattermost":
        message_id = await _post_to_mattermost(context, channel, text)
        return MessagingAlertResult(
            delivered=True, platform=platform, channel_id=channel, message_id=message_id
        ).model_dump()

    from arbora.messaging.service import MessagingService
    from arbora.security.actor_context import use_actor
    from arbora.security.session import use_session

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


#: The engine replays each KV write by its idempotency key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "webhook.memory.subscribe": "medulla:KvCas",
    "webhook.memory.unsubscribe": "medulla:KvDelete",
    "messaging.inbox.send": "medulla:TopicAppend",
}


class WebhookSubscribeParams(Params):
    subscription_id: str = Field(min_length=1, max_length=128)
    url: str = Field(max_length=2048)
    secret_ref: str = Field(max_length=512)
    topics: list[str] = Field(max_length=16)


class WebhookUnsubscribeParams(Params):
    subscription_id: str = Field(min_length=1, max_length=128)


class WebhookListParams(Params):
    pass


class WebhookResult(Params):
    value: dict[str, Any]


def _store(context: Any) -> Any:
    from arbora.api.ops import memory
    from arbora.messaging.memory_subscriptions import SubscriptionStore

    async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
        return await memory._send(context, method, params, key)

    return SubscriptionStore(send)


async def handle_webhook_subscribe(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Create or replace one memory webhook subscription of the caller's tenant."""
    from arbora.messaging.memory_subscriptions import InvalidSubscription

    del op
    try:
        record = await _store(context).subscribe(
            context.caller.tenant,
            subscription_id=params["subscription_id"],
            url=params["url"],
            secret_ref=params["secret_ref"],
            topics=list(params["topics"]),
            idempotency_key=context.idempotency_key,
        )
    except InvalidSubscription as invalid:
        from arbora.api.invoke.pipeline import OperationRefused

        raise OperationRefused("INVALID_ARGUMENT", {"field": invalid.field}) from None
    return {"value": record}


async def handle_webhook_unsubscribe(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    del op
    removed = await _store(context).unsubscribe(
        context.caller.tenant,
        params["subscription_id"],
        idempotency_key=context.idempotency_key,
    )
    return {"value": {"subscription_id": params["subscription_id"], "removed": removed}}


async def handle_webhook_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    del params, op
    return {
        "value": {"subscriptions": await _store(context).list(context.caller.tenant)}
    }


class InboxSendParams(Params):
    recipient_session_id: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=65536)


class InboxReadParams(Params):
    session_id: str = Field(min_length=1, max_length=256)
    cursor: str | None = Field(default=None, max_length=512)
    limit: int = Field(default=50, ge=1, le=200)


class InboxResult(Params):
    value: dict[str, Any]


def _inbox_topic(tenant: str, session_id: str) -> str:
    return f"inbox/{tenant}/{session_id}"


async def _session_props(context: Any, session_id: str) -> Mapping[str, Any] | None:
    """The ``ClientSession`` node of the caller's tenant, or ``None``."""
    from arbora.api.ops.engine_calls import decode, engine_send
    from arbora.api.ops.workstreams import workstream_props as props
    from arbora.identity.client_session import session_node_id

    node = session_node_id(str(context.caller.tenant), session_id)
    rows = decode(
        await engine_send(context, "GetNodePropertiesBatch", {"node_ids": [node]})
    )
    for row in rows or ():
        if isinstance(row, (list, tuple)) and len(row) == 2 and row[0] == node:
            return props(row[1])
    return None


async def _audit_inbox(context: Any, event: Mapping[str, str]) -> None:
    services = context.operation_services
    if services is not None:
        await services.audit_write(dict(event), AuditClass.EVENT, context.caller)


async def handle_inbox_send(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Deliver to a recipient session after sender-to-recipient authorization.

    The sender is the verified caller. An unknown session answers
    ``INBOX_RECIPIENT_UNKNOWN``; a session of another principal the sender may not reach
    answers ``INBOX_SEND_DENIED``. Either refusal enqueues nothing.
    """
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops.pin_guards import send_main

    caller = context.caller
    recipient = await _session_props(context, params["recipient_session_id"])
    if recipient is None:
        raise OperationRefused("INBOX_RECIPIENT_UNKNOWN")
    own = str(recipient.get("principal")) == str(caller.principal)
    if not own and "identity:admin" not in caller.effective_scopes:
        raise OperationRefused("INBOX_SEND_DENIED")
    answer = await send_main(
        context,
        "TopicAppend",
        {
            "topic": _inbox_topic(str(caller.tenant), params["recipient_session_id"]),
            "payload": params["body"],
            "sender": str(caller.principal),
        },
        idempotent=True,
    )
    delivery = dict(answer) if isinstance(answer, Mapping) else {}
    await _audit_inbox(
        context,
        {
            "op": "messaging.inbox.send",
            "sender": str(caller.principal),
            "recipient": str(recipient.get("principal")),
            "recipient_session_id": params["recipient_session_id"],
            "delivery_id": str(delivery.get("offset", delivery.get("id", ""))),
            "tenant": str(caller.tenant),
        },
    )
    return {"value": delivery}


async def handle_inbox_read(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read only the caller's own inbox; a foreign session answers as absent."""
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.api.ops.pin_guards import send_main

    caller = context.caller
    session = await _session_props(context, params["session_id"])
    if session is None or str(session.get("principal")) != str(caller.principal):
        raise OperationRefused("INBOX_RECIPIENT_UNKNOWN")
    request: dict[str, Any] = {
        "topic": _inbox_topic(str(caller.tenant), params["session_id"]),
        "limit": int(params.get("limit", 50)),
    }
    if params.get("cursor"):
        request["cursor"] = params["cursor"]
    answer = await send_main(context, "TopicRead", request)
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


def operations() -> tuple[OpSpec, ...]:
    return (
        build_write_op(
            op_id="webhook.memory.subscribe",
            verb=Verb.MANAGE,
            summary="Subscribe an https endpoint to memory events",
            examples=("subscribe this url to memory events",),
            params=WebhookSubscribeParams,
            result=WebhookResult,
            handler="arbora.api.ops.messaging.handle_webhook_subscribe",
            scope="webhook:manage",
        ),
        build_write_op(
            op_id="webhook.memory.unsubscribe",
            verb=Verb.MANAGE,
            summary="Remove a memory webhook subscription",
            examples=("stop sending memory events to this url",),
            params=WebhookUnsubscribeParams,
            result=WebhookResult,
            handler="arbora.api.ops.messaging.handle_webhook_unsubscribe",
            scope="webhook:manage",
        ),
        build_read_op(
            op_id="webhook.memory.list",
            verb=Verb.FIND,
            summary="List the tenant's memory webhook subscriptions",
            examples=("list my memory webhooks",),
            params=WebhookListParams,
            result=WebhookResult,
            handler="arbora.api.ops.messaging.handle_webhook_list",
            scope="webhook:manage",
        ),
        build_write_op(
            op_id="messaging.inbox.send",
            verb=Verb.ACT,
            summary="Send a message to a client session's inbox",
            examples=("message that session",),
            params=InboxSendParams,
            result=InboxResult,
            handler="arbora.api.ops.messaging.handle_inbox_send",
            scope="bus:write",
        ),
        build_read_op(
            op_id="messaging.inbox.read",
            verb=Verb.ASK,
            summary="Read the messages waiting in the caller's own session inbox",
            examples=("read my session inbox",),
            params=InboxReadParams,
            result=InboxResult,
            handler="arbora.api.ops.messaging.handle_inbox_read",
            scope="bus:read",
        ),
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
    "NATIVE_IDEMPOTENCY",
    "MessagingAlertParams",
    "MessagingAlertResult",
    "handle_messaging_alert",
    "operations",
    "specs",
]
