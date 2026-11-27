"""Hub federation forwards a topic group to a registered peer (ARBORA-A2A-R024).

The engine is a recorded stream double, the peer a loopback transport, and the peer
record comes from the real registry (``ARBORA-A2A-R001.7``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.domain.a2a.hub_federation import (
    FEDERATION_SCOPE_WIDER,
    FederationRefused,
    HubFederation,
    topic_stream,
)
from arbora.api.ops import a2a_peers

TENANT = "tenant-a"


class Engine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.logs: dict[str, list[bytes]] = {
            topic_stream(TENANT, "ops.alerts"): [b"m1", b"m2"]
        }

    async def __call__(self, method: str, params: dict[str, Any], key: Any) -> Any:
        self.calls.append((method, params))
        log = self.logs.setdefault(params["stream"], [])
        if method == "StreamRead":
            return [(i, m) for i, m in enumerate(log) if i >= params["from_offset"]]
        assert method == "StreamPublish"
        log.append(params["payload"])
        return len(log) - 1


class Loopback:
    def __init__(self) -> None:
        self.seen: list[tuple[str, list[bytes]]] = []

    async def forward(self, peer: Any, messages: Any) -> list[bytes]:
        self.seen.append((peer["name"], list(messages)))
        return [b"reply:" + m for m in messages]


def _registry(tmp_path: Path, scope: list[str]) -> a2a_peers.PeerRegistry:
    registry = a2a_peers.PeerRegistry(lambda: tmp_path / "a2a_peers.yml")
    ctx = SimpleNamespace(
        caller=SimpleNamespace(tenant=TENANT, principal="person:one"),
        services={"a2a_peers": registry},
    )
    asyncio.run(
        a2a_peers.handle_peer_register(
            ctx,
            {
                "name": "hub-b",
                "endpoint": "https://hub-b.example.org/a2a",
                "card_digest": "c" * 64,
                "credential_ref": "openbao:kv/a2a/hub-b",
                "topic_scope": scope,
            },
            None,
        )
    )
    return registry


def _federation(registry: Any, engine: Engine, transport: Any) -> HubFederation:
    return HubFederation(engine, registry.lookup, transport, clock=lambda: 1_000.0)


@pytest.mark.spec("ARBORA-A2A-R024")
def test_forward_respects_peer_scope(tmp_path: Path) -> None:
    engine, transport = Engine(), Loopback()
    registry = _registry(tmp_path, ["ops"])
    federation = _federation(registry, engine, transport)

    def forward(**changes: Any) -> Any:
        args = {
            "tenant": TENANT,
            "peer_name": "hub-b",
            "topic": "ops.alerts",
            "caller_scope": ["ops"],
        }
        return asyncio.run(federation.forward(**{**args, **changes}))

    assert forward() == {"forwarded": 2, "replies": 2}
    assert transport.seen == [("hub-b", [b"m1", b"m2"])]
    # The group was read with StreamRead and each reply appended with StreamPublish.
    assert [m for m, _ in engine.calls] == [
        "StreamRead",
        "StreamPublish",
        "StreamPublish",
    ]
    assert engine.calls[1][1]["payload"] == b"reply:m1"
    assert engine.calls[1][1]["now_ms"] == 1_000_000

    def refused(**changes: Any) -> str:
        with pytest.raises(FederationRefused) as caught:
            forward(**changes)
        return caught.value.code

    # A peer registered wider than the caller's scope is refused.
    assert refused(caller_scope=["ops.alerts"]) == FEDERATION_SCOPE_WIDER
    # A topic the intersection does not cover is refused; a foreign tenant's peer is
    # indistinguishable from an absent one.
    assert refused(topic="finance.close") == "TOPIC_NOT_IN_SCOPE"
    assert refused(tenant="tenant-b") == "PEER_UNKNOWN"
    assert refused(peer_name="nobody") == "PEER_UNKNOWN"
    assert len(transport.seen) == 1


@pytest.mark.spec("ARBORA-A2A-R024")
def test_without_a_transport_nothing_is_read_or_appended(tmp_path: Path) -> None:
    engine = Engine()
    federation = _federation(_registry(tmp_path, ["ops"]), engine, None)
    with pytest.raises(FederationRefused) as caught:
        asyncio.run(
            federation.forward(
                tenant=TENANT,
                peer_name="hub-b",
                topic="ops.alerts",
                caller_scope=["ops"],
            )
        )
    assert caught.value.code == "TRANSPORT_UNAVAILABLE" and engine.calls == []
