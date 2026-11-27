"""Hub federation is composed into the host and reaches a peer (ARBORA-A2A-R024).

The operation runs through its real handler over the real peer registry; the peer is
an HTTP loopback behind the SDK A2A client, and the engine is a recorded stream double.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from arbora.a2a import peer_transport
from arbora.a2a.peer_transport import (
    PEER_CREDENTIAL_UNAVAILABLE,
    PEER_UNREACHABLE,
    A2APeerTransport,
)
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.pipeline_services import _declared_owners
from arbora.api.ops import a2a_federation, a2a_peers
from arbora.api.ops.registry_factory import get_registry
from arbora.mcp_server.served_invocation import _with_a2a_peer_transport

TENANT = "tenant-a"
STREAM = f"topic.{TENANT}.ops.alerts"


class Peer:
    """A loopback A2A peer: answers each ``message/send`` with ``echo:<text>``."""

    def __init__(self, *, fail: bool = False) -> None:
        self.auth: list[str | None] = []
        self.texts: list[str] = []
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth.append(request.headers.get("authorization"))
        body = json.loads(request.content)
        if self.fail:
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1}}
            )
        text = body["params"]["message"]["parts"][0]["text"]
        self.texts.append(text)
        task = {
            "kind": "task",
            "id": "t-1",
            "status": {"state": "completed"},
            "history": [
                body["params"]["message"],
                {
                    "kind": "message",
                    "role": "agent",
                    "messageId": "r-1",
                    "parts": [{"kind": "text", "text": f"echo:{text}"}],
                },
            ],
        }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": task}
        )


class Engine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], str | None]] = []
        self.log = [(0, b"m1"), (1, b"m2")]

    async def __call__(
        self, client: Any, method: str, params: dict[str, Any], graph: str, *, key: Any
    ) -> Any:
        self.calls.append((method, params, key))
        return list(self.log) if method == "StreamRead" else len(self.log)


def _transport(peer: Peer, **kwargs: Any) -> A2APeerTransport:
    async def no_sleep(_seconds: float) -> None:
        return None

    return A2APeerTransport(
        resolve_credential=lambda ref: "s3cret",
        inner=httpx.MockTransport(peer),
        sleep=no_sleep,
        **kwargs,
    )


def _context(tmp_path: Path, transport: Any, scope: list[str]) -> Any:
    registry = a2a_peers.PeerRegistry(lambda: tmp_path / "a2a_peers.yml")
    context = SimpleNamespace(
        caller=SimpleNamespace(tenant=TENANT, principal="person:one", engine_claims={}),
        services={
            "a2a_peers": registry,
            "a2a_peer_transport": transport,
            "clock": lambda: 5_000,
        },
        idempotency_key="req-1",
        client=object(),
    )
    asyncio.run(
        a2a_peers.handle_peer_register(
            context,
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
    return context


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    recorded = Engine()

    async def graph(_context: Any) -> str:
        return "graph:one"

    monkeypatch.setattr(a2a_federation, "caller_dispatch_graph", graph)
    monkeypatch.setattr(a2a_federation, "send_on_client", recorded)
    monkeypatch.setattr(a2a_federation, "client_scope", lambda *_a: _Null())
    return recorded


class _Null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: Any) -> None:
        return None


def _forward(context: Any, **changes: Any) -> Any:
    params = {
        "peer": "hub-b",
        "topic": "ops.alerts",
        "topic_scope": ["ops"],
    } | changes
    return asyncio.run(a2a_federation.handle_federation_forward(context, params, None))


@pytest.mark.spec("ARBORA-A2A-R024")
def test_the_operation_forwards_through_the_sdk_client_and_appends_replies(
    tmp_path: Path, engine: Engine
) -> None:
    peer = Peer()
    context = _context(tmp_path, _transport(peer), ["ops"])
    assert _forward(context)["value"] == {"forwarded": 2, "replies": 2}
    assert peer.texts == ["m1", "m2"]
    # The registry reference was resolved and rode as the bearer, never as a param.
    assert set(peer.auth) == {"Bearer s3cret"}
    methods = [(m, k) for m, _p, k in engine.calls]
    assert methods == [
        ("StreamRead", None),
        ("StreamPublish", "req-1:0"),
        ("StreamPublish", "req-1:1"),
    ]
    published = [p for m, p, _k in engine.calls if m == "StreamPublish"]
    assert [p["payload"] for p in published] == [b"echo:m1", b"echo:m2"]
    assert {p["stream"] for p in published} == {STREAM}
    assert published[0]["now_ms"] == 5_000


@pytest.mark.spec("ARBORA-A2A-R024")
def test_scope_refusals_surface_as_closed_codes_with_no_engine_write(
    tmp_path: Path, engine: Engine
) -> None:
    peer = Peer()
    context = _context(tmp_path, _transport(peer), ["ops"])

    def refused(**changes: Any) -> str:
        with pytest.raises(OperationRefused) as caught:
            _forward(context, **changes)
        return caught.value.code

    assert refused(topic_scope=["ops.alerts"]) == "FEDERATION_SCOPE_WIDER"
    assert refused(topic="finance.close") == "TOPIC_NOT_IN_SCOPE"
    assert refused(peer="nobody") == "PEER_UNKNOWN"
    assert engine.calls == [] and peer.texts == []


@pytest.mark.spec("ARBORA-A2A-R024")
def test_an_unanswered_group_appends_nothing(tmp_path: Path, engine: Engine) -> None:
    context = _context(tmp_path, _transport(Peer(fail=True), attempts=2), ["ops"])
    with pytest.raises(OperationRefused) as caught:
        _forward(context)
    assert caught.value.code == PEER_UNREACHABLE
    assert [m for m, _p, _k in engine.calls] == ["StreamRead"]


@pytest.mark.spec("ARBORA-A2A-R024")
def test_an_unresolvable_credential_refuses_before_any_call(
    tmp_path: Path, engine: Engine
) -> None:
    peer = Peer()

    def missing(_ref: str) -> str:
        raise RuntimeError("vault down: kv/a2a/hub-b")

    transport = A2APeerTransport(
        resolve_credential=missing, inner=httpx.MockTransport(peer)
    )
    context = _context(tmp_path, transport, ["ops"])
    with pytest.raises(OperationRefused) as caught:
        _forward(context)
    assert caught.value.code == PEER_CREDENTIAL_UNAVAILABLE
    assert "hub-b" not in repr(caught.value.details) and peer.texts == []


@pytest.mark.spec("ARBORA-A2A-R024")
def test_the_registry_credential_reference_maps_to_the_sdk_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    import scion_sdk.credentials.runtime_references as runtime

    monkeypatch.setattr(
        runtime, "resolve_runtime_secret_reference", lambda ref: seen.append(ref) or "v"
    )
    assert peer_transport.runtime_credential("openbao:kv/a2a/hub-b") == "v"
    assert seen == ["vault://kv/a2a/hub-b"]


@pytest.mark.spec("ARBORA-A2A-R024")
def test_the_host_composes_the_transport_and_the_operation_is_registered() -> None:
    bound = _with_a2a_peer_transport({"fleet_gateway": object()})
    assert isinstance(bound["a2a_peer_transport"], A2APeerTransport)  # type: ignore[index]
    own = object()
    assert (
        _with_a2a_peer_transport({"a2a_peer_transport": own})[  # type: ignore[index]
            "a2a_peer_transport"
        ]
        is own
    )
    assert _with_a2a_peer_transport(None) is None
    assert get_registry().get("a2a.federation.forward") is not None
    assert _declared_owners()["a2a.federation.forward"] == "medulla:StreamPublish"


@pytest.mark.spec("ARBORA-A2A-R024")
def test_without_a_composed_transport_the_operation_refuses_unavailable(
    tmp_path: Path, engine: Engine
) -> None:
    context = _context(tmp_path, None, ["ops"])
    del context.services["a2a_peer_transport"]
    with pytest.raises(OperationRefused) as caught:
        _forward(context)
    assert caught.value.code == "UNAVAILABLE" and engine.calls == []
