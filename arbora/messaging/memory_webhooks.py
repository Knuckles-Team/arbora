"""Outbox pump for memory webhooks (ARBORA-MESSAGING-R700.3, R700.4).

Each tick pulls leases from the engine ``MutationOutbox`` for the consumer
``memory-webhooks``, matches every event to the active subscriptions of the
event's own tenant and topic, delivers each match with ``deliver_with_retry``,
and acknowledges the lease once every match returned ``delivered`` or ``dead``.
An event with no match is acknowledged. A crash before the ack leaves the lease
to expire and the engine redelivers; the stable delivery id lets the receiver
deduplicate. The job is registered with the host co-service supervisor by
``start_memory_webhook_pump`` (``arbora/messaging/supervision.py`` stays a pure
state model with no I/O).
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any

from arbora.core.settings import setting
from arbora.messaging.action_dispatch import HttpPost
from arbora.messaging.memory_subscriptions import (
    TOPICS,
    EngineSend,
    SubscriptionStore,
)
from arbora.messaging.transport import resolve_secret
from arbora.messaging.webhook_delivery import (
    DEAD_LETTER_NAMESPACE,
    RETRY_BASE_S,
    RETRY_CAP_S,
    RETRY_MAX_ATTEMPTS,
    DeliveryRequest,
    Sleep,
    clamp_setting,
    deliver_with_retry,
)

logger = logging.getLogger(__name__)

CONSUMER = "memory-webhooks"
PULL_INTERVAL_S = (2.0, (0.5, 60.0))
PULL_LIMIT = (100, (1, 1000))


def _unwrap(result: Any) -> Any:
    return getattr(result, "payload", result)


def _target(graph: str) -> dict[str, str]:
    return {"target": "graph", "graph": graph}


async def match_subscriptions(
    store: SubscriptionStore, tenant: str, topic: str
) -> list[dict[str, Any]]:
    """Active subscriptions of the event's own tenant that listen to its topic."""
    return await store.active_for(tenant, topic)


async def ack_lease(send: EngineSend, graph: str, lease: Mapping[str, Any]) -> None:
    """Acknowledge one lease to the engine."""
    await send(
        "MutationOutbox",
        {
            "op": {
                "op": "ack",
                "consumer": CONSUMER,
                "lease": dict(lease),
                "target": _target(graph),
            }
        },
        None,
    )


async def pump_once(
    send: EngineSend,
    store: SubscriptionStore,
    *,
    graph: str,
    post: HttpPost | None,
    journal: Callable[[dict[str, Any]], None],
    sleep: Sleep = asyncio.sleep,
    secret_for: Callable[[str], str] = resolve_secret,
    limit: int = 100,
    delivery: Mapping[str, Any] | None = None,
) -> int:
    """Run one pull, deliver and ack tick; return the number of leases settled."""
    config = delivery or {}
    pulled = _unwrap(
        await send(
            "MutationOutbox",
            {
                "op": {
                    "op": "pull",
                    "consumer": CONSUMER,
                    "topics": list(TOPICS),
                    "limit": limit,
                    "target": _target(graph),
                }
            },
            None,
        )
    )
    leases = pulled.get("leases", []) if isinstance(pulled, Mapping) else []
    for lease in leases:
        record = lease["record"]
        topic = record["intent"]["topic"]
        event_key = (
            f"{record['batch_id']}:{record['ordinal']}:{record['intent']['key']}"
        )
        payload = {
            "topic": topic,
            "batch_id": record["batch_id"],
            "key": record["intent"]["key"],
        }
        for sub in await match_subscriptions(
            store, record["identity"]["tenant"], topic
        ):
            await deliver_with_retry(
                DeliveryRequest(
                    url=sub["url"],
                    secret=secret_for(sub["secret_ref"]),
                    subscription_id=sub["subscription_id"],
                    topic=topic,
                    event_key=event_key,
                    payload=payload,
                    post=post,
                    dead_letter=_dead_letter(send, sub["subscription_id"]),
                    journal=journal,
                    sleep=sleep,
                    retry_max_attempts=clamp_setting(
                        config.get("retry_max_attempts"), RETRY_MAX_ATTEMPTS
                    ),
                    retry_base_s=clamp_setting(
                        config.get("retry_base_s"), RETRY_BASE_S
                    ),
                    retry_cap_s=clamp_setting(config.get("retry_cap_s"), RETRY_CAP_S),
                )
            )
        await ack_lease(send, graph, lease)
    return len(leases)


def _dead_letter(send: EngineSend, subscription_id: str) -> Any:
    async def put(entry: dict[str, Any]) -> None:
        raw = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode()
        await send(
            "KvPut",
            {
                "namespace": DEAD_LETTER_NAMESPACE,
                "key": f"{subscription_id}/{entry['delivery_id']}",
                "value": raw,
            },
            None,
        )

    return put


async def run_pump(
    send: EngineSend,
    store: SubscriptionStore,
    stop: threading.Event,
    *,
    graph: str,
    post: HttpPost | None,
    journal: Callable[[dict[str, Any]], None],
    sleep: Sleep = asyncio.sleep,
    config: Mapping[str, Any] | None = None,
) -> None:
    """Tick until ``stop`` is set; one failed tick never ends the job."""
    cfg = config or {}
    interval = clamp_setting(cfg.get("pull_interval_s"), PULL_INTERVAL_S)
    limit = clamp_setting(cfg.get("pull_limit"), PULL_LIMIT)
    while not stop.is_set():
        try:
            await pump_once(
                send,
                store,
                graph=graph,
                post=post,
                journal=journal,
                sleep=sleep,
                limit=limit,
                delivery=cfg,
            )
        except Exception as exc:  # the lease expires and the engine redelivers
            logger.warning("memory webhook tick failed (%s)", type(exc).__name__)
        await sleep(interval)


def start_memory_webhook_pump(supervisor: Any, engine: Any, session: Any) -> None:
    """Register the supervised ``memory-webhook-pump`` job on the host."""

    def run(stop: threading.Event) -> None:
        from arbora.security.session import use_session

        routed = getattr(engine, "_session_client", None)
        client = routed() if callable(routed) else engine

        async def send(method: str, params: dict[str, Any], key: str | None) -> Any:
            with use_session(session):
                return await client._send(method, params, None, idempotency_key=key)

        graph = str(getattr(session, "graph", "") or setting("KG_DEFAULT_GRAPH", ""))
        journal = lambda entry: logger.info("memory webhook %s", json.dumps(entry))  # noqa: E731
        asyncio.run(
            run_pump(
                send,
                SubscriptionStore(send),
                stop,
                graph=graph,
                post=_http_post(),
                journal=journal,
            )
        )

    supervisor.start_service("memory-webhook-pump", run, session)


def _http_post() -> HttpPost:
    import httpx
    from scion_sdk.tls.resolve import resolve_tls_profile

    from arbora.messaging.webhook_delivery import (
        TIMEOUT_S,
        canonical_body,
        clamp_setting,
    )

    timeout = clamp_setting(None, TIMEOUT_S)

    def post(
        method: str, url: str, body: dict[str, Any], headers: dict[str, str]
    ) -> int:
        trust = resolve_tls_profile("memory-webhooks")
        try:
            options = {
                **trust.httpx_kwargs(),
                "follow_redirects": False,
                "timeout": timeout,
            }
            with httpx.Client(**options) as client:
                return client.request(
                    method, url, content=canonical_body(body), headers=headers
                ).status_code
        finally:
            trust.cleanup()

    return post
