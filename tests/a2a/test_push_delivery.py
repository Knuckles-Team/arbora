"""Push notifications register on a task and deliver from the engine queue (ARBORA-A2A-R013).

The engine is an in-memory ``work_items`` double; time is the injected clock and nothing
sleeps. Registration validates the target and stores it on the caller's own task; a
terminal transition submits one ``a2a_push`` item per target; the worker claims, signs,
posts, defers with backoff and dead-letters.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from typing import Any
from unittest.mock import patch

import pytest

from arbora.a2a.push import (
    PUSH_KIND,
    TARGETS_KEY,
    PushRefused,
    PushSettings,
    validate_push_url,
)
from arbora.security.session import use_session
from tests.a2a._push_support import ITEM_ID, TASK_ID, Target, seed_task, world

URL = "https://hooks.example.test/a2a"


def _register(
    host: Any, url: str = URL, token_ref: str | None = "vault:a2a/token"
) -> Any:
    with use_session(host.who):
        return host.worker.register_push_target(TASK_ID, url, token_ref)


async def _terminal(host: Any) -> None:
    host.items.rows[ITEM_ID]["status"] = "succeeded"
    with use_session(host.who):
        await host.authority.runs._publish(
            ITEM_ID,
            None,
            task_id=TASK_ID,
            context_id="ctx",
            kind="status-update",
            state="completed",
            final=True,
        )


def _run(host: Any) -> list[tuple[str, str]]:
    return asyncio.run(host.worker.run_once((host.who,)))


def _pushes(host: Any) -> list[str]:
    return [i for i, r in host.items.rows.items() if r["kind"] == PUSH_KIND]


@pytest.mark.spec("ARBORA-A2A-R013.1")
@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://hooks.example.test/a2a", "PUSH_URL_INVALID"),
        ("https://" + "a" * 2050, "PUSH_URL_INVALID"),
        (
            "https://" + "user" + ":secret" + "@hooks.example.test/a2a",
            "PUSH_URL_INVALID",
        ),
        ("https://127.0.0.1/a2a", "PUSH_TARGET_PRIVATE"),
        ("https://[::1]/a2a", "PUSH_TARGET_PRIVATE"),
        ("https://10.1.2.3/a2a", "PUSH_TARGET_PRIVATE"),
        ("https://169.254.169.254/latest", "PUSH_TARGET_PRIVATE"),
        ("https://[::ffff:127.0.0.1]/a2a", "PUSH_TARGET_PRIVATE"),
    ],
)
def test_registration_refuses_private_and_non_https_targets(
    url: str, code: str
) -> None:
    host = world()
    seed_task(host)
    with pytest.raises(PushRefused) as refused:
        _register(host, url)
    assert refused.value.code == code
    assert TARGETS_KEY not in host.items.rows[ITEM_ID]["metadata"]


@pytest.mark.spec("ARBORA-A2A-R013.1")
def test_a_hostname_resolving_private_is_refused_unless_allowlisted() -> None:
    def private(name: str) -> list[str]:
        return ["10.0.0.7"]

    with pytest.raises(PushRefused) as refused:
        validate_push_url(URL, resolve=private)
    assert refused.value.code == "PUSH_TARGET_PRIVATE"
    assert (
        validate_push_url(
            URL, allowed_private_hosts=("hooks.example.test",), resolve=private
        )
        == "hooks.example.test"
    )

    def unresolvable(name: str) -> list[str]:
        raise OSError("dns")

    host = world(resolve=unresolvable)
    seed_task(host)
    with pytest.raises(PushRefused) as refused:
        _register(host)
    assert refused.value.code == "PUSH_URL_INVALID"


@pytest.mark.spec("ARBORA-A2A-R013.1")
def test_registration_stores_the_target_and_a_foreign_task_reads_as_absent() -> None:
    host = world()
    seed_task(host)
    target = _register(host)
    assert target.task_id == TASK_ID
    assert host.items.rows[ITEM_ID]["metadata"][TARGETS_KEY] == [
        {"url": URL, "token_ref": "vault:a2a/token"}
    ]
    # Registering the same URL again replaces it; it does not duplicate it.
    _register(host, token_ref="vault:a2a/other")
    assert len(host.items.rows[ITEM_ID]["metadata"][TARGETS_KEY]) == 1
    # A task another owner holds answers like an absent one and stores nothing.
    host.items.rows[ITEM_ID]["metadata"]["a2a_owner_ref"] = "someone-else"
    before = json.dumps(host.items.rows[ITEM_ID]["metadata"], sort_keys=True)
    with pytest.raises(LookupError):
        _register(host, "https://other.example.test/a2a")
    assert json.dumps(host.items.rows[ITEM_ID]["metadata"], sort_keys=True) == before


@pytest.mark.spec("ARBORA-A2A-R013.2")
def test_terminal_transition_submits_one_push_item_per_target_and_delivers_signed() -> (
    None
):
    target = Target(200)
    host = world(post=target)
    seed_task(host)
    _register(host)
    _register(host, "https://second.example.test/a2a", None)
    asyncio.run(_terminal(host))
    assert len(_pushes(host)) == 2
    # A repeated terminal publish is idempotent: the same keys, no new items.
    asyncio.run(_terminal(host))
    assert len(_pushes(host)) == 2

    assert [outcome for _id, outcome in _run(host)] == ["committed", "committed"]
    _url, headers, body = next(p for p in target.posts if p[0] == URL)
    assert body["id"] == TASK_ID and body["status"]["state"] == "completed"
    assert headers["Authorization"] == "Bearer token-for-vault:a2a/token"
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(
        b"token-for-vault:a2a/token",
        headers["X-Arbora-Timestamp"].encode() + b"." + canonical,
        hashlib.sha256,
    ).hexdigest()
    assert headers["X-Arbora-Signature"] == expected
    assert "Authorization" not in next(p for p in target.posts if p[0] != URL)[1]
    assert _run(host) == []


@pytest.mark.spec("ARBORA-A2A-R013.2")
def test_failed_delivery_defers_then_dead_letters() -> None:
    target = Target(503)
    host = world(post=target, max_attempts=3)
    seed_task(host)
    _register(host)
    asyncio.run(_terminal(host))
    [item_id] = _pushes(host)
    row = host.items.rows[item_id]

    assert _run(host) == [(item_id, "deferred")]
    assert row["not_before_ms"] == int((host.clock.now + 5) * 1000)
    # Not yet due on the injected clock: the engine grants no claim.
    assert _run(host) == []
    host.clock.now += 5
    assert _run(host) == [(item_id, "deferred")]
    assert row["not_before_ms"] == int((host.clock.now + 10) * 1000)
    host.clock.now += 10
    assert _run(host) == [(item_id, "committed")]
    assert (row["status"], row["error_ref"]) == ("failed", "PUSH_DEAD_LETTER")
    assert len(target.posts) == 3
    assert _run(host) == []


@pytest.mark.spec("ARBORA-A2A-R013.2")
def test_backoff_is_capped_and_a_private_resolution_at_delivery_is_never_posted() -> (
    None
):
    target = Target(200)
    host = world(post=target, max_attempts=10)
    assert host.worker._backoff_s(0) == 5
    assert host.worker._backoff_s(1) == 10
    assert host.worker._backoff_s(9) == 20  # the cap
    seed_task(host)
    _register(host)
    asyncio.run(_terminal(host))
    host.worker._resolve = lambda name: [
        "192.168.1.9"
    ]  # the name moved after registering
    [(_id, outcome)] = _run(host)
    assert outcome == "deferred" and target.posts == []


@pytest.mark.spec("ARBORA-A2A-R013.2")
def test_settings_are_clamped_configuration() -> None:
    values = {
        "A2A_PUSH_MAX_ATTEMPTS": "99",
        "A2A_PUSH_BACKOFF_BASE_S": "0",
        "A2A_PUSH_ALLOWED_PRIVATE_HOSTS": "Hooks.Example.Test, other.example.test",
    }
    with patch.dict(os.environ, values):
        config = PushSettings.from_config()
    assert (config.max_attempts, config.backoff_base_s) == (20, 1)
    assert config.backoff_cap_s == 600 and config.timeout_s == 10
    assert config.allowed_private_hosts == ("hooks.example.test", "other.example.test")


@pytest.mark.spec("ARBORA-A2A-R013.2")
def test_host_starts_the_push_service_only_with_a_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from arbora.a2a.push import PushDeliveryService
    from arbora.mcp_server.served_background import _push_service

    host = world()
    runtime = SimpleNamespace(
        bindings={"a2a": SimpleNamespace(authority=host.authority)}
    )
    assert isinstance(_push_service(runtime, lambda: host.who), PushDeliveryService)
    host.authority.push = None
    assert _push_service(runtime, lambda: host.who) is None
    assert _push_service(SimpleNamespace(bindings={}), lambda: host.who) is None


@pytest.mark.spec("ARBORA-A2A-R013.2")
def test_service_polls_until_stopped_on_the_injected_sleep() -> None:
    from arbora.a2a.push import PushDeliveryService

    target = Target(200)
    host = world(post=target)
    seed_task(host)
    _register(host)
    asyncio.run(_terminal(host))
    waits: list[float] = []
    polls = {"n": 0}

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        polls["n"] += 1

    service = PushDeliveryService(
        host.worker, lambda: host.who, sleep=sleep, poll_interval_s=7.0
    )
    asyncio.run(service.run(lambda: polls["n"] >= 2))
    assert waits == [7.0, 7.0]
    assert len(target.posts) == 1
