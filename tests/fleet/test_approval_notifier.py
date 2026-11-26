"""Approver notices from the outbox and the scheduled digest (ARBORA-OPS-R918.5, R918.6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import approvals
from arbora.fleet import outbox_consumer
from arbora.fleet.approval_notifier import (
    APPROVALS_TOPIC,
    approval_handler,
    digest_delivery,
    role_holders,
)
from arbora.fleet.outbox_consumer import consume_topic
from arbora.messaging.action_dispatch import NotificationSpec

USERS = [
    {"principal_id": "p:ed1", "roles": ["editors"], "status": "active"},
    {"principal_id": "p:ed2", "roles": ["editors", "admin"], "status": "active"},
    {"principal_id": "p:gone", "roles": ["editors"], "status": "disabled"},
    {"principal_id": "p:boss", "roles": ["admin"], "status": "active"},
]
EVENT = {
    "approval_id": "action_approval:one",
    "gate_class": "publishing",
    "approver_roles": ["editors"],
    "expires_at_ms": 123,
    "preview": "SECRET BODY",
}


class Notifier:
    def __init__(self, fail: set[str] | None = None) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail or set()

    def notify(self, spec: NotificationSpec, message: str) -> dict[str, Any]:
        if spec.recipient in self.fail:
            raise RuntimeError("channel down")
        self.sent.append((spec.recipient, message))
        return {"delivered": True}


class Engine:
    """A recording engine: identity users, and an outbox that keeps unacked leases."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.leases = [
            {
                "attempt": 1,
                "consumer": "c",
                "lease_epoch": 1,
                "lease_until_ms": 1,
                "record": {
                    "intent": {
                        "topic": APPROVALS_TOPIC,
                        "key": f"k{i}",
                        "headers": {},
                        "payload": msgpack.packb(event),
                    }
                },
            }
            for i, event in enumerate(events)
        ]
        self.acked: list[str] = []

    async def send(self, method: str, params: dict[str, Any]) -> Any:
        op = params["op"]
        if method == "Identity":
            return {"kind": "users", "value": USERS}
        if op["op"] == "pull":
            return {"leases": list(self.leases)}
        self.acked.append(op["lease"]["record"]["intent"]["key"])
        self.leases = [x for x in self.leases if x is not op["lease"]]
        return {}


@pytest.fixture
def pull_served(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(outbox_consumer, "_pull_served", lambda: True)


# spec: ARBORA-OPS-R918.6
def test_notice_per_approver_without_preview_and_retries_from_outbox(
    pull_served: None,
) -> None:
    engine = Engine([EVENT])
    notifier = Notifier(fail={"p:ed2"})
    handle = approval_handler(role_holders(engine.send), notifier)

    async def pull() -> int:
        return await consume_topic(
            engine.send,
            graph="g",
            consumer="approvals",
            topic=APPROVALS_TOPIC,
            handle=handle,
        )

    # A failed send acknowledges nothing: the event stays in the outbox.
    assert asyncio.run(pull()) == 0
    assert engine.acked == [] and len(engine.leases) == 1

    notifier.fail.clear()
    notifier.sent.clear()
    assert asyncio.run(pull()) == 1
    assert engine.acked == ["k0"]
    # One notice per active holder; no disabled user, no preview or secret.
    assert sorted(r for r, _ in notifier.sent) == ["p:ed1", "p:ed2"]
    for _, message in notifier.sent:
        assert "action_approval:one" in message and "publishing" in message
        assert "123" in message and "SECRET" not in message


# spec: ARBORA-OPS-R918.6
def test_outbox_pull_refuses_unavailable_on_an_engine_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(outbox_consumer, "_pull_served", lambda: False)
    engine = Engine([EVENT])

    async def attempt() -> None:
        await consume_topic(
            engine.send,
            graph="g",
            consumer="c",
            topic=APPROVALS_TOPIC,
            handle=approval_handler(role_holders(engine.send), Notifier()),
        )

    with pytest.raises(OperationRefused) as refused:
        asyncio.run(attempt())
    assert refused.value.code == "UNAVAILABLE" and engine.acked == []


class _Leases:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def list(self, *, status: str, **_kw: Any) -> dict[str, Any]:
        return {"leases": [r for r in self.rows if r["status"] == status]}


def _row(suffix: str, gate_class: str, issued: int) -> dict[str, Any]:
    return {
        "lease_id": f"action_approval:{suffix}",
        "kind": "action.approval",
        "status": "active",
        "revision": 1,
        "issued_at_ms": issued,
        "expires_at_ms": 10_000,
        "hard_expires_at_ms": 10_000,
        "grant": {
            "request_digest": suffix,
            "op_id": "op",
            "proposer": "a",
            "gate_class": gate_class,
            "approver_roles": ["editors"],
        },
    }


def _digest_context(rows: list[dict[str, Any]], notifier: Notifier) -> SimpleNamespace:
    return SimpleNamespace(
        caller=SimpleNamespace(principal="s", tenant="t", roles=()),
        client=SimpleNamespace(control_leases=_Leases(rows)),
        services={
            "now_ms": lambda: 5_000,
            "approval_digest_delivery": digest_delivery(
                role_holders(Engine([]).send), notifier
            ),
        },
    )


_ROWS = [
    ("a", "publishing", 1_000),
    ("b", "publishing", 4_000),
    ("c", "money_movement", 2_000),
    ("old", "publishing", 10),
]
_BY_CLASS = [
    {"gate_class": "money_movement", "pending": 1, "oldest_age_ms": 3_000},
    {"gate_class": "publishing", "pending": 2, "oldest_age_ms": 4_000},
]


# spec: ARBORA-OPS-R918.5
def test_digest_read_counts_by_class_and_has_no_effects() -> None:
    notifier = Notifier()
    context = _digest_context([_row(*r) for r in _ROWS], notifier)
    read = asyncio.run(
        approvals.handle_approvals_digest(
            context,
            {"since_ms": 100},
            None,  # type: ignore[arg-type]
        )
    )
    assert read == {"by_class": _BY_CLASS, "cursor_ms": 5_000}
    assert notifier.sent == []
    spec = {op.id: op for op in approvals.operations()}["approvals.digest"]
    assert spec.effect.value == "read"
    assert "deliver" not in spec.params.model_fields


# spec: ARBORA-OPS-R918.5.1
def test_digest_send_delivers_one_notice_per_owner_and_refuses_without_a_channel() -> (
    None
):
    notifier = Notifier()
    context = _digest_context([_row(*r) for r in _ROWS], notifier)
    sent = asyncio.run(
        approvals.handle_approvals_digest_send(
            context,
            {"since_ms": 100},
            None,  # type: ignore[arg-type]
        )
    )
    assert sent["by_class"] == _BY_CLASS
    assert sent["cursor_ms"] == 5_000 and sent["delivered"] == 2
    assert sorted(r for r, _ in notifier.sent) == ["p:boss", "p:ed2"]
    assert all("SECRET" not in m and "publishing: 2" in m for _, m in notifier.sent)
    spec = {op.id: op for op in approvals.operations()}["approvals.digest.send"]
    assert spec.verb.value == "act" and spec.effect.value == "write"

    context.services.pop("approval_digest_delivery")
    with pytest.raises(OperationRefused) as unbound:
        asyncio.run(
            approvals.handle_approvals_digest_send(
                context,
                {"since_ms": 0},
                None,  # type: ignore[arg-type]
            )
        )
    assert unbound.value.code == "UNAVAILABLE"


# spec: ARBORA-OPS-R918.6
def test_bootstrap_builds_both_consumers_and_a_tick_runs_each_once(
    pull_served: None,
) -> None:
    from arbora.api.invoke.bootstrap import build_outbox_consumers
    from arbora.fleet.outbox_consumer import tick

    engine = Engine([EVENT])
    consumers = build_outbox_consumers(engine.send, Notifier())
    assert [c.topic for c in consumers] == ["catalog.trust_flag", APPROVALS_TOPIC]
    done = asyncio.run(tick(consumers, engine.send, graph="g"))
    assert set(done) == {"catalog.trust_flag", APPROVALS_TOPIC}
