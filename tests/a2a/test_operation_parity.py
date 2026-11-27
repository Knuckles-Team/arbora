"""Three-surface parity of one operation (ARBORA-A2A-R006.2, ARBORA-A2A-R006.2.2).

A read, a write and a fleet operation are invoked through the real A2A route
(``arbora.op/invoke``), the MCP verbs and the REST route. Each surface composes its own
copy of the same registry, so the shared pipeline is the only thing the three have in
common. The decisions must match for scope, policy, authentication, effect count and
audit: a refused call causes zero effects on every surface, and the refusal code is the
same code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from arbora.a2a.application import create_a2a_application
from arbora.a2a.service import A2AService
from arbora.api.http.app import create_api_application
from arbora.api.http.auth import AmbientHTTPAuthenticator
from arbora.api.invoke import invoke
from arbora.api.invoke.pipeline_services import FleetCallDecision
from arbora.api.mcp.caller import caller_from_session
from arbora.api.mcp.verbs import MCPProjection, dispatch_verb
from arbora.api.ops.fleet import operations as fleet_operations
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import (
    Confirm,
    Effect,
    Executor,
    Idempotency,
    PrincipalRule,
    Registry,
    Surface,
    Verb,
)
from arbora.core.settings import RuntimeConfig
from arbora.observability.state import SubsystemWindows
from tests.a2a.test_application import Authenticator, Authority, Router
from tests.a2a.test_operation_bridge import DenyPolicy, SessionRuntime, session
from tests.api.test_invoke_contract import operation, setup_services

pytestmark = [
    pytest.mark.spec("ARBORA-A2A-R006.2"),
    pytest.mark.spec("ARBORA-A2A-R006.2.2"),
]

TOKEN = "fixture-verified"  # sanitizer:ignore - synthetic credential
KEY = "effect:parity"


@dataclass(frozen=True)
class Case:
    """One operation to invoke: its id, the scope it needs and its MCP verb."""

    op_id: str
    scope: str
    verb: str
    params: dict[str, Any]


def _read() -> Any:
    return operation(
        id="example.read",
        verb=Verb.ASK,
        effect=Effect.READ,
        idempotency=Idempotency.NONE,
        scopes=frozenset({"example:read"}),
        surfaces=frozenset(Surface),
    )


def _fleet() -> Any:
    [call] = [op for op in fleet_operations() if op.id == "fleet.call"]
    return call


CASES = {
    "read": Case("example.read", "example:read", "ask", {"value": 7}),
    "write": Case("example.write", "example:write", "write", {"value": 7}),
    "fleet": Case(
        "fleet.call",
        "mcp:delegate",
        "act",
        {"server": "files", "tool": "read", "arguments": {}},
    ),
}


async def fleet_effect(_op: Any, _params: Any, _caller: Any) -> FleetCallDecision:
    """The live catalog's decision for the one fleet child call under test."""
    return FleetCallDecision(
        effect=Effect.WRITE,
        confirm=Confirm.NONE,
        principals=PrincipalRule.ANY,
        executor=Executor.CALLER,
        required_scopes=frozenset({"mcp:delegate"}),
        executor_scopes=frozenset(),
        subject_id=None,
        credential_mode="delegated",
    )


class Effects(SessionRuntime):
    """The scripted effect port: one answer per operation, every call recorded."""

    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        self.calls.append((op, params, context))
        return {"value": 7}


@dataclass
class Outcome:
    """What one surface decided: its code, how many effects ran, what it audited."""

    code: str
    effects: int
    audits: list[tuple[str, str]]


class Surfaces:
    """One shared registry composed three times, with each copy's own effect ports."""

    def __init__(
        self, bound: Any, *, policy: PolicyGate | None, authentic: bool = True
    ) -> None:
        self.bound, self.authentic = bound, authentic
        self.parts: dict[str, Any] = {}
        registry = Registry([operation(), _read(), _fleet()])
        for name in ("a2a", "http", "mcp"):
            services, _runtime, audit, journal, _leases = setup_services(
                None,
                registry=registry,
                external_effect_operations=frozenset(op.id for op in registry),
                fleet_effect=fleet_effect,
                **({"policy_gate": policy} if policy is not None else {}),
            )
            runtime = Effects()
            self.parts[name] = (replace(services, runtime=runtime), runtime, audit)

    def outcome(self, name: str, code: str) -> Outcome:
        _services, runtime, audit = self.parts[name]
        entries = [(e["op"], e["principal"]) for e in audit.events if "op" in e]
        return Outcome(code, len(runtime.calls), entries)

    async def _resolve(self, request: Any) -> Any:
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            raise PermissionError("Unverified request")
        return self.bound

    def _token(self) -> str:
        return TOKEN if self.authentic else "unverified"  # sanitizer:ignore - synthetic

    async def a2a(self, case: Case) -> Outcome:
        services = self.parts["a2a"][0]
        app = create_a2a_application(
            service=A2AService(
                authority=Authority(), router=Router(), config=RuntimeConfig()
            ),
            authenticator=Authenticator(),
            operation_services=services,
            operation_authenticator=AmbientHTTPAuthenticator(
                session_for_request=self._resolve
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            response = await client.post(
                "/a2a",
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "X-Request-Id": "request:parity",
                },
                json={
                    "jsonrpc": "2.0",
                    "id": "parity",
                    "method": "arbora.op/invoke",
                    "params": {
                        "op": case.op_id,
                        "params": case.params,
                        "idempotencyKey": KEY,
                    },
                },
            )
        body = response.json()
        refused = body.get("error", {}).get("data", {}).get("error", {})
        ok = body.get("result", {}).get("ok") is True
        return self.outcome("a2a", "OK" if ok else str(refused.get("code")))

    async def http(self, case: Case) -> Outcome:
        services = self.parts["http"][0]

        async def visible(_op: Any, _caller: Any) -> bool:
            return True

        app = create_api_application(
            services=services,
            authenticator=AmbientHTTPAuthenticator(session_for_request=self._resolve),
            invoke=invoke,
            visibility=visible,
            config=RuntimeConfig(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fixture"
        ) as client:
            response = await client.post(
                f"/ops/{case.op_id}",
                headers={
                    "Authorization": f"Bearer {self._token()}",
                    "Idempotency-Key": KEY,
                },
                json=case.params,
            )
        body = response.json()
        code = "OK" if response.status_code == 200 else body["error"]["code"]
        return self.outcome("http", code)

    async def mcp(self, case: Case) -> Outcome:
        services = self.parts["mcp"][0]
        caller = (
            caller_from_session(self.bound, credential_kind="bearer")
            if self.authentic
            else None
        )
        projection = MCPProjection(
            services.registry,
            services,
            SimpleNamespace(scope_ref=lambda *args, **kwargs: "scope"),
            lambda: caller,
            services.policy_gate,
            invoke,
            windows=SubsystemWindows(),
        )
        body = await dispatch_verb(
            case.verb,
            projection,
            op=case.op_id,
            params=case.params,
            idempotency_key=KEY,
        )
        code = "OK" if body.get("ok") else str(body["error"]["code"])
        return self.outcome("mcp", code)

    async def all(self, case: Case) -> dict[str, Outcome]:
        return {
            "a2a": await self.a2a(case),
            "http": await self.http(case),
            "mcp": await self.mcp(case),
        }


def _run(
    case: Case,
    *,
    scopes: frozenset[str] | None = None,
    policy: PolicyGate | None = None,
    authentic: bool = True,
) -> dict[str, Outcome]:
    bound = session(frozenset({case.scope}) if scopes is None else scopes)
    return asyncio.run(Surfaces(bound, policy=policy, authentic=authentic).all(case))


def _same(outcomes: dict[str, Outcome]) -> Outcome:
    """The one decision all three surfaces share, or a failure naming the split."""
    codes = {name: (o.code, o.effects, tuple(o.audits)) for name, o in outcomes.items()}
    assert len(set(codes.values())) == 1, codes
    return outcomes["a2a"]


@pytest.mark.parametrize("name", sorted(CASES))
def test_read_write_fleet_match_across_surfaces(name: str) -> None:
    allowed = _same(_run(CASES[name]))
    # The same authority runs the effect exactly once on every surface.
    assert allowed.code == "OK" and allowed.effects == 1
    # An effect is audited once, as the same principal, on every surface.
    assert bool(allowed.audits) == (CASES[name].verb != "ask")
    refusals: dict[str, dict[str, Outcome]] = {
        "scope": _run(CASES[name], scopes=frozenset()),
        "policy": _run(CASES[name], policy=PolicyGate("remote", DenyPolicy())),
        "credential": _run(CASES[name], authentic=False),
    }
    expected = {
        "scope": "SCOPE_REQUIRED",
        "policy": "POLICY_DENIED",
        "credential": "UNAUTHENTICATED",
    }
    for failure, outcomes in refusals.items():
        refused = _same(outcomes)
        # A refused call is the same refusal code with zero effects everywhere.
        assert (refused.code, refused.effects) == (expected[failure], 0), failure
