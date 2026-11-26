"""An in-memory engine double and caller doubles for the hosted-session tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.identity.client_session import session_node_id

TENANT = "tenant:one"


def caller(
    principal: str = "person:ada",
    tenant: str = TENANT,
    scopes: frozenset[str] = frozenset({"identity:self"}),
    **changes: Any,
) -> SimpleNamespace:
    base = {
        "principal": principal,
        "tenant": tenant,
        "effective_scopes": scopes,
        "authenticated": True,
        "principal_kind": "human",
        "engine_claims": {},
        "request_id": "req-1",
        "credential_kind": "bearer",
    }
    base.update(changes)
    return SimpleNamespace(**base)


class Engine:
    """Nodes by id; records every send."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.topics: dict[str, list[dict[str, Any]]] = {}

    def add_session(self, principal: str, session_id: str, tenant: str = TENANT) -> str:
        node = session_node_id(tenant, session_id)
        self.nodes[node] = {
            "principal": principal,
            "tenant": tenant,
            "session_id": session_id,
            "client": "cli",
            "started_at_ms": 1,
        }
        return node

    async def send(
        self, context: Any, method: str, params: dict[str, Any], **_: Any
    ) -> Any:
        self.sent.append((method, params))
        if method == "GetNodePropertiesBatch":
            return [
                [n, msgpack.packb(self.nodes[n])]
                for n in params["node_ids"]
                if n in self.nodes
            ]
        if method == "GetNodesByLabel":
            return [[n, msgpack.packb(p)] for n, p in self.nodes.items()]
        if method == "CompareAndSetNodeFields":
            self.nodes[params["node_id"]].update(
                msgpack.unpackb(params["updates_msgpack"])
            )
            return True
        if method == "TopicAppend":
            self.topics.setdefault(params["topic"], []).append(params)
            return {"offset": len(self.topics[params["topic"]])}
        if method == "TopicRead":
            return {"messages": self.topics.get(params["topic"], [])}
        raise AssertionError(method)

    def create_node(self, node_id: str, label: str, properties: Any = None) -> bool:
        if node_id in self.nodes:
            return False
        self.nodes[node_id] = dict(properties or {}, label=label)
        return True

    def install(self, monkeypatch: pytest.MonkeyPatch) -> Engine:
        monkeypatch.setattr("arbora.api.ops._engine.engine_send", self.send)
        monkeypatch.setattr("arbora.pin_lag.medulla.send_main", self.send_main)
        return self

    async def send_main(
        self, context: Any, method: str, params: dict, **kw: Any
    ) -> Any:
        return await self.send(context, method, params, **kw)


def context(who: SimpleNamespace, **extra: Any) -> SimpleNamespace:
    events: list[Any] = []

    async def write(event: Any, cls: Any, c: Any) -> None:
        events.append(event)

    return SimpleNamespace(
        caller=who,
        services=extra.pop("services", {}),
        operation_services=SimpleNamespace(audit_write=write),
        idempotency_key="key-1",
        client=None,
        events=events,
        **extra,
    )
