"""The outbound A2A peer registry is served as operations (ARBORA-A2A-R001.7).

The handlers run on the served-invocation context; the registry file is a temporary
``a2a_peers.yml`` under a controlled clock. A second tenant proves that a foreign
peer answers exactly like an absent one.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.pipeline_services import _declared_owners
from arbora.api.ops import a2a_peers
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Surface

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


def _context(registry: a2a_peers.PeerRegistry, tenant: str = "tenant-a") -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant=tenant, principal=f"person:{tenant}"),
        services={"a2a_peers": registry},
        idempotency_key="key-1",
    )


def _params(**changes: Any) -> dict[str, Any]:
    base = {
        "name": "hub",
        "endpoint": "https://hub.example.org/a2a",
        "card_digest": DIGEST_A,
        "skills": ["search", "plan"],
        "credential_ref": "openbao:kv/a2a/hub",
    }
    return {**base, **changes}


def _call(handler: Any, context: Any, params: dict[str, Any]) -> Any:
    return asyncio.run(handler(context, params, None))["value"]


@pytest.fixture
def registry(tmp_path: Path) -> a2a_peers.PeerRegistry:
    return a2a_peers.PeerRegistry(lambda: tmp_path / "a2a_peers.yml", Clock())


@pytest.mark.spec("ARBORA-A2A-R001.7")
def test_register_refresh_list_delete_through_invoke(
    registry: a2a_peers.PeerRegistry, tmp_path: Path
) -> None:
    ctx = _context(registry)
    first = _call(a2a_peers.handle_peer_register, ctx, _params())
    assert first["name"] == "hub" and len(first["revisions"]) == 1
    # A refresh with the same digest replaces the record and appends no revision.
    same = _call(a2a_peers.handle_peer_register, ctx, _params(skills=["plan"]))
    assert same["skills"] == ["plan"] and len(same["revisions"]) == 1
    assert same["refreshed_at"] > first["refreshed_at"]
    # A changed digest appends one with the caller and the time.
    moved = _call(a2a_peers.handle_peer_register, ctx, _params(card_digest=DIGEST_B))
    assert [r["card_digest"] for r in moved["revisions"]] == [DIGEST_A, DIGEST_B]
    assert moved["revisions"][1]["caller"] == "person:tenant-a"
    _call(a2a_peers.handle_peer_register, ctx, _params(name="alpha"))
    listed = _call(a2a_peers.handle_peer_list, ctx, {})["peers"]
    assert [p["name"] for p in listed] == ["alpha", "hub"]
    # The peers persist under the key ``peers`` and survive a new registry (restart).
    document = yaml.safe_load((tmp_path / "a2a_peers.yml").read_text())
    assert [row["name"] for row in document["peers"]] == ["alpha", "hub"]
    reopened = a2a_peers.PeerRegistry(lambda: tmp_path / "a2a_peers.yml")
    assert [p["name"] for p in reopened.list("tenant-a")] == ["alpha", "hub"]
    assert _call(a2a_peers.handle_peer_delete, ctx, {"name": "hub"}) == {
        "deleted": "hub"
    }
    assert [p["name"] for p in registry.list("tenant-a")] == ["alpha"]


@pytest.mark.spec("ARBORA-A2A-R001.7")
def test_refusals_and_the_revision_cap(registry: a2a_peers.PeerRegistry) -> None:
    ctx = _context(registry)

    def refused(**changes: Any) -> str:
        with pytest.raises(OperationRefused) as caught:
            _call(a2a_peers.handle_peer_register, ctx, _params(**changes))
        return caught.value.code

    assert refused(credential_ref="sk-live-abcdef") == "PEER_CREDENTIAL_VALUE"
    assert refused(credential_ref="Bearer abc.def") == "PEER_CREDENTIAL_VALUE"
    assert refused(endpoint="http://hub.example.org/a2a") == "PEER_ENDPOINT_INSECURE"
    assert refused(endpoint="ftp://hub.example.org") == "PEER_ENDPOINT_INSECURE"
    # Loopback may use plain http.
    assert (
        _call(
            a2a_peers.handle_peer_register,
            ctx,
            _params(name="local", endpoint="http://127.0.0.1:9000/a2a"),
        )["name"]
        == "local"
    )
    for index in range(60):
        digest = f"{index:064x}"
        last = _call(a2a_peers.handle_peer_register, ctx, _params(card_digest=digest))
    assert len(last["revisions"]) == a2a_peers.MAX_REVISIONS
    assert last["revisions"][-1]["card_digest"] == f"{59:064x}"


@pytest.mark.spec("ARBORA-A2A-R001.7")
def test_a_foreign_tenants_peer_answers_like_an_absent_one(
    registry: a2a_peers.PeerRegistry,
) -> None:
    mine, theirs = _context(registry, "tenant-a"), _context(registry, "tenant-b")
    _call(a2a_peers.handle_peer_register, mine, _params())
    assert _call(a2a_peers.handle_peer_list, theirs, {})["peers"] == []
    assert registry.lookup("tenant-b", "hub") is None
    with pytest.raises(OperationRefused) as foreign:
        _call(a2a_peers.handle_peer_delete, theirs, {"name": "hub"})
    with pytest.raises(OperationRefused) as absent:
        _call(a2a_peers.handle_peer_delete, theirs, {"name": "nothing"})
    assert (foreign.value.code, foreign.value.details) == (
        absent.value.code,
        absent.value.details,
    )
    assert [p["name"] for p in registry.list("tenant-a")] == ["hub"]


@pytest.mark.spec("ARBORA-A2A-R001.7")
def test_ops_are_registered_on_http_and_mcp_with_a_declared_replay_owner() -> None:
    registry = get_registry()
    for op_id in ("a2a.peer.register", "a2a.peer.delete", "a2a.peer.list"):
        op = registry.get(op_id)
        assert op is not None
        assert {Surface.MCP, Surface.HTTP} <= set(op.surfaces)
    owners = _declared_owners()
    assert owners["a2a.peer.register"] and owners["a2a.peer.delete"]


@pytest.mark.spec("ARBORA-A2A-R001.7")
def test_a_lookup_among_ten_thousand_peers_is_fast(tmp_path: Path) -> None:
    rows = [
        {"tenant": "tenant-a", "name": f"peer-{i}", "revisions": []}
        for i in range(10_000)
    ]
    path = tmp_path / "a2a_peers.yml"
    path.write_text(yaml.safe_dump({"peers": rows}))
    registry = a2a_peers.PeerRegistry(lambda: path)
    assert registry.lookup("tenant-a", "peer-0") is not None
    samples = []
    for i in range(0, 10_000, 10):
        start = time.perf_counter_ns()
        registry.lookup("tenant-a", f"peer-{i}")
        samples.append(time.perf_counter_ns() - start)
    samples.sort()
    assert samples[int(len(samples) * 0.99)] < 200_000
