"""Shared fakes for tests that drive EG through generated senders.

The fake engine answers at ``client._send``, so every generated sender, model
and decoder runs for real. A subclass answers one method or ConnectorPack op
with an ``_on_<name>`` handler.
"""

from __future__ import annotations

from typing import Any

from arbora.access.action_policy import (
    ActionDecision,
    ActionRequest,
    PolicyDisposition,
    PolicyReceipt,
)
from arbora.security.actor_context import ActorContext, ActorType
from arbora.security.session import GraphSession


class FakeEgEngine:
    """One fake EG transport shared by every graph view."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.graph_compute = self

    def for_graph(self, _graph: str) -> Any:
        return type("View", (), {"async_client": self})()

    async def _send(
        self, method: str, params: Any, graph: Any, *, idempotency_key: Any = None
    ) -> Any:
        op = (params or {}).get("op") if method == "ConnectorPack" else None
        name = op["op"] if isinstance(op, dict) else method
        self.calls.append((name, graph, params))
        handler = getattr(self, f"_on_{name}".replace("-", "_"), None)
        if handler is None:
            raise AssertionError(f"unexpected EG call {name}")
        return handler(params, idempotency_key)


class AllowPolicy:
    """An action policy that approves every request."""

    def decide(self, request: ActionRequest) -> ActionDecision:
        return ActionDecision(
            decision="allow",
            tier="auto_notify",
            request=request,
            receipt=PolicyReceipt(
                receipt_id="action_decision:test",
                request_digest=request.digest(),
                disposition=PolicyDisposition.APPROVE,
                policy_origin="test",
            ),
        )


def service_session(tenant: str, graph: str) -> GraphSession:
    """Arbora's verified process session with the pack-import scopes."""

    return GraphSession(
        actor=ActorContext(
            actor_id="service:arbora",
            actor_type=ActorType.AUTOMATED_SERVICE,
            tenant_id=tenant,
            authenticated=True,
        ),
        tenant=tenant,
        scopes=frozenset({"agent:pack-control", "connector:catalog-attest"}),
        graph=graph,
        audience="arbora",
        policy_version="policy-a",
    )


class SyncControlLeases:
    """An in-memory engine control-lease store with the blocking view's shape.

    Mirrors the engine's rules the approval path depends on: an existing id is
    a ``collision``, a transition is compare-and-set on ``revision`` along the
    legal edges, and a listing filters by tenant, kind, status and exact
    top-level grant pairs.
    """

    _EDGES = {
        ("active", "consumed"),
        ("active", "revoked"),
        ("active", "expired"),
        ("consumed", "revoked"),
        ("consumed", "expired"),
    }

    def __init__(self, tenant: str = "test") -> None:
        self.tenant = tenant
        self.rows: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def issue(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("issue", request))
        assert request["tenant"] == self.tenant
        if request["lease_id"] in self.rows:
            return {"outcome": "collision", "lease": None}
        lease = {
            "lease_id": request["lease_id"],
            "kind": request["kind"],
            "status": "active",
            "grant": dict(request["grant"]),
            "issued_at_ms": request["issued_at_ms"],
            "expires_at_ms": request["expires_at_ms"],
            "hard_expires_at_ms": request["hard_expires_at_ms"],
            "revision": 1,
        }
        self.rows[lease["lease_id"]] = lease
        return {"outcome": "issued", "lease": dict(lease)}

    def get(self, *, tenant: str, lease_id: str) -> dict[str, Any] | None:
        self.calls.append(("get", {"tenant": tenant, "lease_id": lease_id}))
        lease = self.rows.get(lease_id) if tenant == self.tenant else None
        return None if lease is None else dict(lease)

    def list(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("list", request))
        match = request.get("grant_match") or {}
        leases = [
            dict(lease)
            for lease in self.rows.values()
            if request["tenant"] == self.tenant
            and request.get("kind") in (None, lease["kind"])
            and request.get("status") in (None, lease["status"])
            and all(lease["grant"].get(k) == v for k, v in match.items())
        ]
        return {"leases": leases, "next_cursor": None}

    def transition(self, **request: Any) -> dict[str, Any]:
        self.calls.append(("transition", request))
        lease = self.rows.get(request["lease_id"])
        if lease is None or request["tenant"] != self.tenant:
            return {"outcome": "not_found", "lease": None}
        edge = (lease["status"], request["to"])
        if lease["revision"] != request["expected_revision"] or edge not in self._EDGES:
            return {"outcome": "conflict", "lease": dict(lease)}
        lease["status"] = request["to"]
        lease["revision"] += 1
        return {"outcome": "applied", "lease": dict(lease)}
