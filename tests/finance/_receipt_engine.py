"""A loopback engine node store for the finance receipt tests.

``ReceiptEngine`` answers the two public engine-client calls the served
``ServedBrowserState`` authority makes -- ``nodes.create_if_absent`` and
``query.cypher_read`` -- with ``CreateNodeIfAbsent`` semantics, partitioned by
the tenant of the verified client context. Its node table outlives any one
host, so a test can drop the store and the authority (a host restart) and
read the same receipts back.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any

from arbora.finance.receipt_store import FinanceReceiptStore
from arbora.mcp_server.browser_state import ServedBrowserState
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.session import GraphSession

_ID = re.compile(r'\{id: "([0-9a-f]{64})"\}')


class ReceiptEngine:
    """Engine-resident nodes keyed by ``(tenant, node_id)``."""

    def __init__(self) -> None:
        self.nodes: dict[tuple[str, str], dict[str, Any]] = {}
        self.fail = False

    def pool(self) -> _Pool:
        return _Pool(self)


class _Pool:
    def __init__(self, engine: ReceiptEngine) -> None:
        self._engine = engine

    async def connected_client(self, context: Any, _graph: str) -> _Client:
        return _Client(self._engine, context.tenant)


class _Client:
    authenticated = True

    def __init__(self, engine: ReceiptEngine, tenant: str) -> None:
        self.nodes = self
        self.query = self
        self._engine = engine
        self._tenant = tenant

    def use_verified_context(self, _context: Any) -> Any:
        return contextlib.nullcontext()

    async def create_if_absent(self, node_id: str, properties: dict[str, Any]) -> bool:
        if self._engine.fail:
            raise ConnectionError("engine unreachable")
        key = (self._tenant, node_id)
        if key in self._engine.nodes:
            return False
        self._engine.nodes[key] = dict(properties)
        return True

    async def cypher_read(self, query: str) -> list[dict[str, Any]]:
        if self._engine.fail:
            raise ConnectionError("engine unreachable")
        match = _ID.search(query)
        node = self._engine.nodes.get((self._tenant, match.group(1))) if match else None
        return [] if node is None else [{"state": node["receiptState"]}]


def receipt_store(engine: ReceiptEngine) -> FinanceReceiptStore:
    """The store exactly as invocation composition builds it, over ``engine``."""

    return FinanceReceiptStore(ServedBrowserState(engine.pool()))


def tenant_session(tenant: str) -> GraphSession:
    """A verified caller session of ``tenant`` with graph read and write."""

    return GraphSession(
        actor=ActorContext(
            actor_id=f"user:{tenant}",
            actor_type=ActorType.HUMAN,
            tenant_id=tenant,
            authenticated=True,
        ),
        tenant=tenant,
        scopes=frozenset({"kg:read", "kg:write"}),
        graph=tenant,
        audience="arbora",
        policy_version="policy-a",
    )
