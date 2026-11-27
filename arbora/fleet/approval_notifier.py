"""Notify approver-role holders of a gated plan (ARBORA-OPS-R918.6).

The engine announces each filed approval item on the outbox topic
``approvals.pending``. This consumer sends one notice to each holder of an
approver role. The notice names the item, the class and the expiry and carries
no preview or secret. Role holders are read from the engine identity store on
every event, so Arbora keeps no subscription table. A failed send leaves the
event in the outbox for a retry and never blocks the plan.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.api.ops.engine_calls import decode
from arbora.fleet.outbox_consumer import (
    Handler,
    OutboxEvent,
    Send,
    notify_each,
)
from arbora.messaging.action_dispatch import Notifier

__all__ = [
    "APPROVALS_TOPIC",
    "OWNER_ROLE",
    "approval_handler",
    "digest_delivery",
    "digest_text",
    "role_holders",
]

#: The role whose holders receive the scheduled approvals digest.
OWNER_ROLE = "admin"

APPROVALS_TOPIC = "approvals.pending"
_PAGE = 100
_MAX_PAGES = 64

#: ``holders(role)`` returns the principals that hold one role.
RoleHolders = Callable[[str], Awaitable[list[str]]]


def role_holders(send: Send) -> RoleHolders:
    """Read role holders from the engine identity users listing."""

    async def holders(role: str) -> list[str]:
        found: list[str] = []
        after: str | None = None
        for _ in range(_MAX_PAGES):
            request: dict[str, Any] = {"limit": _PAGE}
            if after is not None:
                request["after"] = after
            reply = decode(
                await send(
                    "Identity",
                    {"op": {"family": "user", "op": "list", "request": request}},
                )
            )
            users = (reply or {}).get("value") or []
            found += [
                str(user["principal_id"])
                for user in users
                if role in (user.get("roles") or ()) and user.get("status") == "active"
            ]
            if len(users) < _PAGE:
                break
            after = str(users[-1]["principal_id"])
        return found

    return holders


def notice_text(payload: Mapping[str, Any]) -> str:
    """The notice: item, class and expiry only."""
    return (
        f"Approval pending: item {payload.get('approval_id')}, class "
        f"{payload.get('gate_class')}, expires at {payload.get('expires_at_ms')}."
    )


def approval_handler(holders: RoleHolders, notifier: Notifier) -> Handler:
    async def handle(event: OutboxEvent) -> None:
        roles = event.payload.get("approver_roles") or []
        recipients: list[str] = []
        for role in roles:
            recipients += await holders(str(role))
        notify_each(notifier, recipients, notice_text(event.payload))

    return handle


def digest_text(by_class: list[Mapping[str, Any]]) -> str:
    """The digest notice: counts and oldest age per class, no item content."""
    lines = [
        f"{row['gate_class']}: {row['pending']} pending, oldest {row['oldest_age_ms']} ms"
        for row in by_class
    ]
    return "Approvals digest. " + "; ".join(lines)


def digest_delivery(
    holders: RoleHolders, notifier: Notifier
) -> Callable[[list[Mapping[str, Any]]], Awaitable[int]]:
    """Deliver one digest notice to each tenant owner; no item is dropped."""

    async def deliver(by_class: list[Mapping[str, Any]]) -> int:
        owners = await holders(OWNER_ROLE)
        return notify_each(notifier, owners, digest_text(by_class))

    return deliver
