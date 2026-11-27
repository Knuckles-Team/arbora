"""The resource gate at fleet.call refuses by default (ARBORA-IDENTITY-R019.3 to R019.6)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.access.resource_gate import PUBLIC_DENIAL, ResourceGate
from arbora.security.operation_result import OpError

NOW = 1_800_000_000.0
CALLER = SimpleNamespace(
    tenant="t1", principal="ada", client="cli", request_id="r1", surface=None
)
OP = SimpleNamespace(id="fleet.call", executor=SimpleNamespace(value="caller"))


class Fixture:
    """Engine decisions by resource, and connectors that record connection attempts."""

    def __init__(self) -> None:
        self.decisions: dict[str, dict[str, Any]] = {}
        self.attempts: list[tuple[str, str]] = []
        self.events: list[dict[str, str]] = []
        self.engine_down = False
        self.gate = ResourceGate(
            decide=self._decide,
            clock=lambda: NOW,
            emit=self.events.append,
            trusted_roles=lambda _caller: ("dba",),
        )

    async def _decide(self, request: Any) -> dict[str, Any]:
        if self.engine_down:
            raise RuntimeError("engine unreachable")
        return self.decisions.get(request.resource_ref, {"allow": False})

    def grant(self, resource: str, **changes: Any) -> None:
        self.decisions[resource] = {
            "allow": True,
            "grant_id": f"g:{resource}",
            "expires_at_ms": int(NOW * 1000) + 60_000,
            "revocation_revision": 4,
            "scope_ceiling": ["x"],
            **changes,
        }

    def call(self, server: str, protocol: str | None, resource: str) -> bool:
        """One fleet.call: the connector connects only when the gate lets it through."""
        params = {"server": server, "tool": "run", "arguments": {"target": resource}}
        decision = SimpleNamespace(
            resource_protocol=protocol, resource_param="target" if protocol else None
        )
        refused = asyncio.run(self.gate(OP, params, CALLER, decision))
        self.last = refused
        if refused is None:
            self.attempts.append((server, resource))
        return refused is None


@pytest.mark.spec("ARBORA-IDENTITY-R019.3")
@pytest.mark.spec("ARBORA-IDENTITY-R019.4")
@pytest.mark.spec("ARBORA-IDENTITY-R019.6")
def test_served_protocol_grants_default_deny() -> None:
    fx = Fixture()
    fx.grant("web:one")
    fx.grant("ssh:one")
    fx.grant("db:one", subject="role:dba")
    fx.grant("rdp:one")
    assert fx.call("browser-agent", "web", "web:one")
    assert fx.call("tunnel-manager", "ssh", "ssh:one")
    assert fx.call("sql-mcp", "database", "db:one")
    permitted = list(fx.attempts)
    # A tool with no resource declaration skips the gate (the shared policy stays).
    assert fx.call("other", None, "anything")
    fx.attempts.remove(("other", "anything"))
    # Unrelated resource, no grant, rdp (no connector enforces it) and a connector that
    # does not enforce the protocol: zero connections, one public code.
    for server, protocol, resource in (
        ("browser-agent", "web", "web:two"),
        ("tunnel-manager", "ssh", "ssh:two"),
        ("sql-mcp", "rdp", "rdp:one"),
        ("generic-http", "ssh", "ssh:one"),
        ("sql-mcp", "database", ""),
    ):
        assert not fx.call(server, protocol, resource)
        assert isinstance(fx.last, OpError)
    assert fx.attempts == permitted
    assert fx.last.code == PUBLIC_DENIAL
    fx.call("sql-mcp", "rdp", "rdp:one")
    assert fx.last.code == "UNSUPPORTED_ENFORCEMENT"
    # Revoked (engine now denies), expired and stale grants end open access.
    fx.decisions["web:one"] = {"allow": False}
    assert (
        not fx.call("browser-agent", "web", "web:one") and fx.last.code == PUBLIC_DENIAL
    )
    fx.grant("web:one", expires_at_ms=int(NOW * 1000))
    assert (
        not fx.call("browser-agent", "web", "web:one") and fx.last.code == PUBLIC_DENIAL
    )
    codes = {e.get("refusal_code") for e in fx.events}
    assert {
        "RESOURCE_GRANT_MISSING",
        "RESOURCE_UNKNOWN",
        "UNSUPPORTED_ENFORCEMENT",
        "GRANT_EXPIRED",
    } <= codes
    # An unreadable engine refuses; it never allows.
    fx.engine_down = True
    assert not fx.call("browser-agent", "web", "web:one")
    assert fx.last.code == "AUTHORITY_UNAVAILABLE"
    assert fx.attempts == permitted


@pytest.mark.spec("ARBORA-IDENTITY-R019.5")
def test_evidence_joins_decision_and_enforcement() -> None:
    fx = Fixture()
    fx.grant("db:one")
    assert fx.call("sql-mcp", "database", "db:one")
    [event] = fx.events
    assert (
        event["principal"] == "ada"
        and event["tenant"] == "t1"
        and event["client"] == "cli"
    )
    assert event["resource_ref"] == "db:one" and event["protocol"] == "database"
    assert (
        event["resource_operation"] == "run"
        and event["engine_decision_ref"] == "g:db:one"
    )
    assert event["enforcement"] == "permitted" and "grant_expires_at" in event
    assert event["request_id"] == "r1"
    blob = repr(event)
    assert (
        event["target"] == "sql-mcp/run"
        and "payload" not in blob
        and "password" not in blob
    )
