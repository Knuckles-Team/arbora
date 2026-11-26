"""Memory webhook subscriptions, signed delivery and the outbox pump (ARBORA-MESSAGING-R700)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import messaging as ops
from arbora.messaging.memory_subscriptions import TOPICS, SubscriptionStore
from arbora.messaging.memory_webhooks import pump_once
from arbora.messaging.webhook_delivery import (
    DeliveryRequest,
    deliver_with_retry,
    sign_payload,
)


class FakeEngine:
    """A KV namespace plus a MutationOutbox with scripted leases."""

    def __init__(self, leases: list[dict[str, Any]] | None = None) -> None:
        self.kv: dict[tuple[str, str], tuple[bytes, int]] = {}
        self.leases = list(leases or [])
        self.acked: list[dict[str, Any]] = []
        self.crash_before_ack = False

    async def send(self, method: str, params: dict[str, Any], key: str | None) -> Any:
        if method == "KvGet":
            entry = self.kv.get((params["namespace"], params["key"]))
            return {"value": entry[0], "version": entry[1]} if entry else {}
        if method in {"KvPut", "KvCas"}:
            value = params.get("value", params.get("new"))
            slot = (params["namespace"], params["key"])
            version = self.kv.get(slot, (b"", 0))[1]
            if method == "KvCas" and params["expected_version"] != version:
                return {"swapped": False, "version": version}
            self.kv[slot] = (value, version + 1)
            return {"swapped": True, "version": version + 1}
        if method == "KvDelete":
            return self.kv.pop((params["namespace"], params["key"]), None) is not None
        if method == "KvScan":
            rows = [
                {"key": k, "value": v, "version": ver}
                for (ns, k), (v, ver) in self.kv.items()
                if ns == params["namespace"] and k.startswith(params["prefix"])
            ]
            return {"entries": rows}
        assert method == "MutationOutbox"
        op = params["op"]
        if op["op"] == "pull":
            assert op["consumer"] == "memory-webhooks"
            return {"leases": list(self.leases)}
        if self.crash_before_ack:
            raise RuntimeError("crash")
        self.acked.append(op["lease"])
        self.leases = [x for x in self.leases if x != op["lease"]]
        return {}


def _lease(tenant: str, topic: str, ordinal: int) -> dict[str, Any]:
    return {
        "attempt": 0,
        "consumer": "memory-webhooks",
        "lease_epoch": 1,
        "lease_until_ms": 1,
        "record": {
            "batch_id": f"b{ordinal}",
            "ordinal": ordinal,
            "identity": {"tenant": tenant},
            "intent": {
                "topic": topic,
                "key": f"k{ordinal}",
                "headers": {},
                "payload": b"",
            },
        },
    }


def _context(engine: FakeEngine, tenant: str = "t1") -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant=tenant, session=None),
        idempotency_key="idem-1",
        engine=engine,
    )


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    engine = FakeEngine()
    from arbora.api.ops import memory

    async def send(context: Any, method: str, params: dict[str, Any], key: Any) -> Any:
        return await context.engine.send(method, params, key)

    monkeypatch.setattr(memory, "_send", send)
    return engine


def _params(**over: Any) -> dict[str, Any]:
    base = {
        "subscription_id": "s1",
        "url": "https://hooks.example.org/in",
        "secret_ref": "openbao://kv/hooks/s1",
        "topics": list(TOPICS),
    }
    return {**base, **over}


@pytest.mark.spec("ARBORA-MESSAGING-R700.1")
async def test_subscribe_list_unsubscribe_and_refusals(served: FakeEngine) -> None:
    ctx = _context(served)
    spec = ops.operations()
    assert [(o.id, o.verb.value) for o in spec] == [
        ("webhook.memory.subscribe", "manage"),
        ("webhook.memory.unsubscribe", "manage"),
        ("webhook.memory.list", "find"),
    ]
    assert all(o.scopes == frozenset({"webhook:manage"}) for o in spec)
    assert set(ops.NATIVE_IDEMPOTENCY) == {o.id for o in spec[:2]}

    made = await ops.handle_webhook_subscribe(ctx, _params(), spec[0])
    assert made["value"]["active"] is True and "secret" not in json.dumps(made)
    listed = await ops.handle_webhook_list(ctx, {}, spec[2])
    assert [s["subscription_id"] for s in listed["value"]["subscriptions"]] == ["s1"]
    other = await ops.handle_webhook_list(_context(served, "t2"), {}, spec[2])
    assert other["value"]["subscriptions"] == []

    for field, bad in (("url", "http://insecure.example.org"), ("topics", ["nope"])):
        with pytest.raises(OperationRefused) as refused:
            await ops.handle_webhook_subscribe(ctx, _params(**{field: bad}), spec[0])
        assert refused.value.code == "INVALID_ARGUMENT"
        assert refused.value.details["field"] == field

    gone = await ops.handle_webhook_unsubscribe(ctx, {"subscription_id": "s1"}, spec[1])
    assert gone["value"]["removed"] is True
    assert (await ops.handle_webhook_list(ctx, {}, spec[2]))["value"][
        "subscriptions"
    ] == []


class _Receiver:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.calls: list[tuple[str, dict[str, Any], dict[str, str]]] = []

    def post(
        self, method: str, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> int:
        self.calls.append((url, body, headers))
        return self.statuses.pop(0) if self.statuses else 200


@pytest.mark.spec("ARBORA-MESSAGING-R700.2")
async def test_signature_retry_backoff_and_dead_letter() -> None:
    delays: list[float] = []
    dead: list[dict[str, Any]] = []
    journal: list[dict[str, Any]] = []

    async def sleep(delay: float) -> None:
        delays.append(delay)

    async def dead_letter(entry: dict[str, Any]) -> None:
        dead.append(entry)

    receiver = _Receiver([500, 503, 500])

    def make(post: Any, attempts: int = 3) -> DeliveryRequest:
        return DeliveryRequest(
            url="https://hooks.example.org/in",
            secret="s3cret",  # sanitizer:ignore - synthetic test fixture
            subscription_id="s1",
            topic=TOPICS[0],
            event_key="e1",
            payload={"a": 1},
            post=post,
            dead_letter=dead_letter,
            journal=journal.append,
            sleep=sleep,
            clock=lambda: 1_000.0,
            retry_max_attempts=attempts,
            retry_base_s=2.0,
            retry_cap_s=3.0,
        )

    assert await deliver_with_retry(make(receiver.post)) == "dead"
    assert delays == [2.0, 3.0]  # base * 2**attempt, capped
    assert len(dead) == 1 and dead[0]["attempts"] == 3
    url, body, headers = receiver.calls[0]
    assert headers["X-Arbora-Signature"] == sign_payload("s3cret", "1000", b'{"a":1}')
    assert {"X-Arbora-Timestamp", "X-Arbora-Event", "X-Arbora-Delivery"} <= set(headers)
    assert "s3cret" not in json.dumps(journal)

    ok = _Receiver([200])
    assert await deliver_with_retry(make(ok.post)) == "delivered"
    # The delivery id is stable per event and subscription.
    assert ok.calls[0][2]["X-Arbora-Delivery"] == headers["X-Arbora-Delivery"]


async def _subscribed(engine: FakeEngine, tenant: str = "t1") -> SubscriptionStore:
    store = SubscriptionStore(engine.send)
    await store.subscribe(tenant, **_params())
    return store


async def _no_sleep(_: float) -> None:
    await asyncio.sleep(0)


@pytest.mark.spec("ARBORA-MESSAGING-R700.3")
async def test_pump_pulls_delivers_and_acks_once() -> None:
    engine = FakeEngine([_lease("t1", TOPICS[0], 1), _lease("t1", "other.topic", 2)])
    store = await _subscribed(engine)
    receiver = _Receiver([200])
    settled = await pump_once(
        engine.send,
        store,
        graph="g",
        post=receiver.post,
        journal=lambda e: None,
        sleep=_no_sleep,
        secret_for=lambda ref: "s3cret",
    )
    assert settled == 2
    assert len(receiver.calls) == 1  # the unmatched event is acknowledged, not sent
    assert len(engine.acked) == 2
    again = await pump_once(
        engine.send,
        store,
        graph="g",
        post=receiver.post,
        journal=lambda e: None,
        sleep=_no_sleep,
        secret_for=lambda ref: "s3cret",
    )
    assert again == 0 and len(receiver.calls) == 1


@pytest.mark.spec("ARBORA-MESSAGING-R700.3")
async def test_crash_before_ack_redelivers_with_the_same_delivery_id() -> None:
    engine = FakeEngine([_lease("t1", TOPICS[0], 1)])
    store = await _subscribed(engine)
    receiver = _Receiver([])
    engine.crash_before_ack = True
    with pytest.raises(RuntimeError):
        await pump_once(
            engine.send,
            store,
            graph="g",
            post=receiver.post,
            journal=lambda e: None,
            sleep=_no_sleep,
            secret_for=lambda ref: "s3cret",
        )
    engine.crash_before_ack = False
    await pump_once(
        engine.send,
        store,
        graph="g",
        post=receiver.post,
        journal=lambda e: None,
        sleep=_no_sleep,
        secret_for=lambda ref: "s3cret",
    )
    ids = [headers["X-Arbora-Delivery"] for _, _, headers in receiver.calls]
    assert len(ids) == 2 and ids[0] == ids[1]
    assert len(engine.acked) == 1


@pytest.mark.spec("ARBORA-MESSAGING-R700.4")
async def test_turn_to_signed_webhook_journey() -> None:
    engine = FakeEngine([_lease("t1", topic, i) for i, topic in enumerate(TOPICS)])
    store = await _subscribed(engine)
    receiver = _Receiver([])
    journal: list[dict[str, Any]] = []
    await pump_once(
        engine.send,
        store,
        graph="g",
        post=receiver.post,
        journal=journal.append,
        sleep=_no_sleep,
        secret_for=lambda ref: "s3cret",
    )
    assert sorted(
        headers["X-Arbora-Event"] for _, _, headers in receiver.calls
    ) == sorted(TOPICS)
    assert {e["outcome"] for e in journal} == {"delivered"}
    assert all(
        set(e)
        == {
            "delivery_id",
            "subscription_id",
            "topic",
            "outcome",
            "attempts",
            "status_code",
        }
        for e in journal
    )


@pytest.mark.spec("ARBORA-MESSAGING-R700.4")
async def test_other_tenant_events_never_match() -> None:
    engine = FakeEngine([_lease("t2", TOPICS[0], 1)])
    store = await _subscribed(engine, "t1")
    receiver = _Receiver([])
    await pump_once(
        engine.send,
        store,
        graph="g",
        post=receiver.post,
        journal=lambda e: None,
        sleep=_no_sleep,
        secret_for=lambda ref: "s3cret",
    )
    assert receiver.calls == []
    await store.unsubscribe("t1", "s1")
    engine.leases = [_lease("t1", TOPICS[0], 2)]
    await pump_once(
        engine.send,
        store,
        graph="g",
        post=receiver.post,
        journal=lambda e: None,
        sleep=_no_sleep,
        secret_for=lambda ref: "s3cret",
    )
    assert receiver.calls == []  # a removed subscription stops at the next tick
