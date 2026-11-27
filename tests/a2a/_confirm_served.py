"""A confirm chain served over ``POST /a2a`` with the production ``ask_decide`` seam.

The grants live in an in-memory control-lease double with the engine's revision
compare-and-set semantics; the ``Decide`` engine is a loopback that answers like the
admission families do. Everything between the route and the engine is production code:
``create_a2a_application``, ``confirm_dispatch``, ``compose_approval_chain`` and
``build_ask_decide``.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Mapping
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.confirm import ApprovalChain, RegisteredKeyProducer, emit_input_required
from arbora.a2a.engine_decide import build_ask_decide
from arbora.a2a.engine_deciders import EngineHeldItemDecider, EngineToolActionDecider
from arbora.a2a.grant_store import HumanGrantStore
from arbora.a2a.service import A2AService
from arbora.api.domain.a2a.held_item_admission import HeldItemAdmitter
from arbora.api.domain.a2a.tool_action_admission import ToolActionAdmitter
from arbora.api.invoke.plan import PlanBinding
from arbora.core.claims import params_digest
from arbora.core.settings import RuntimeConfig
from arbora.identity.approval import RegisteredApprovalKey
from tests._support import unwrap_decide
from tests.a2a.test_application import Authority, Router
from tests.a2a.test_grant_store import Client

TENANT = "tenant:one"
HUMAN = "person:approver"
SERVICE = "service:arbora"
SCHEMA = {"schema_version": 1, "body": "pin"}
POLICY = "revision:one"
PARAMS = params_digest({"value": 1})


class Leases(Client):
    """Control leases with the async ``issue/get/transition`` port the store reads."""


class Keys:
    def __init__(self) -> None:
        self.private: dict[str, Ed25519PrivateKey] = {}
        self.down = False

    def register(self, who: str) -> Ed25519PrivateKey:
        self.private[who] = Ed25519PrivateKey.generate()
        return self.private[who]

    async def resolve_key(
        self, key_id: str, *, principal_id: str, purpose: str
    ) -> RegisteredApprovalKey:
        if self.down:
            raise ConnectionError("identity authority unreachable")
        public = self.private[principal_id].public_key().public_bytes_raw().hex()
        return RegisteredApprovalKey(
            key_id,
            TENANT,
            principal_id,
            1,
            public,
            4_000_000_000_000,
            None,
            "signed_approval",
        )


class Loopback:
    """A ``Decide`` engine answering by question kind and the reference it governs."""

    def __init__(self) -> None:
        self.answers: dict[str, Any] = {}
        self.requests: list[dict[str, Any]] = []

    async def __call__(self, method: str, params: dict[str, Any], tenant: str) -> Any:
        assert method == "Decide" and tenant == TENANT
        params = unwrap_decide(params)
        self.requests.append(params)
        by_name = {p["name"]: p["value"]["value"] for p in params["params"]}
        anchor = by_name.get("pending_call_id") or by_name["item_ref"]
        return {
            "records": [
                {
                    "record_id": f"rec:{anchor}",
                    "record_digest": "d" * 64,
                    "caller_principal": "principal:sha256:"
                    + hashlib.sha256(SERVICE.encode()).hexdigest(),
                    "tenant_id": params["tenant_id"],
                    "question": params["question"],
                    "inputs": {"params": params["params"]},
                    "answer": self.answers[anchor],
                }
            ]
        }


class Audit:
    def __init__(self) -> None:
        self.entries: list[Mapping[str, Any]] = []

    async def __call__(self, entry: Mapping[str, Any]) -> None:
        self.entries.append(entry)


class Served:
    """The chain, its doubles and the route."""

    def __init__(self) -> None:
        self.leases, self.keys = Leases(), Keys()
        self.engine, self.effects = Loopback(), []
        self.audit = Audit()
        self.clock = [1_800_000_000_000]
        self.chain: ApprovalChain = self._compose()
        self.key = self.keys.register(HUMAN)
        self.keys.register("person:other")

    def _compose(self) -> ApprovalChain:
        async def client() -> Any:
            return object()

        async def effect(binding: Any) -> Mapping[str, Any]:
            self.effects.append(binding.pending_call_id)
            return {"applied": binding.pending_call_id}

        ask = build_ask_decide(
            self.engine,
            graph="g",
            feature_schema=lambda tenant: SCHEMA,
            principal=lambda: SERVICE,
        )
        return ApprovalChain(
            HumanGrantStore(
                self.leases,
                lease_kind="arbora.human_grant",
                clock_ms=lambda: self.clock[0],
            ),
            RegisteredKeyProducer(self.keys, clock_ms=lambda: self.clock[0]),
            effect,
            self.audit,
            clock_ms=lambda: self.clock[0],
            tool_actions=ToolActionAdmitter(EngineToolActionDecider(ask), self.audit),
            held_items=HeldItemAdmitter(EngineHeldItemDecider(ask), self.audit),
        )

    def caller(self, principal: str = HUMAN, tenant: str = TENANT) -> Any:
        """A verified human caller carrying its admitted session."""
        import time

        from arbora.api.mcp.caller import caller_from_session
        from arbora.security.actor_context import (
            ActorContext,
            ActorType,
            CredentialLease,
        )
        from arbora.security.request_identity import _resolve_authenticated_scopes
        from arbora.security.session import GraphSession
        from tests.test_real_boot import ROLES

        actor = ActorContext(
            actor_id=principal,
            actor_type=ActorType.HUMAN,
            roles=ROLES,
            tenant_id=tenant,
            authenticated=True,
            credential_lease=CredentialLease(int(time.time()) + 600),
        )
        session = GraphSession(
            actor=actor,
            tenant=tenant,
            scopes=_resolve_authenticated_scopes(actor),
            graph="__control__",
            audience="arbora-test",
            policy_version=POLICY,
        )
        return caller_from_session(session, credential_kind="session", request_id="r1")

    def plan(self) -> PlanBinding:
        return PlanBinding(
            "tool.write",
            PARAMS,
            HUMAN,
            TENANT,
            POLICY,
            params_digest({"r": 1}),
            "write",
            "plan",
        )

    async def pend(self, task: str, call: str, ref: str = "plan:one") -> str:
        """Issue the durable grant and return the human's signature over its binding."""
        projection = await emit_input_required(
            self.chain,
            task_id=task,
            parent_work_item_id="work:parent",
            child_work_item_id="work:child",
            pending_call_id=call,
            plan_ref=ref,
            plan=self.plan(),
        )
        payload = projection["pending_call"]["signing_payload"]
        return base64.urlsafe_b64encode(self.key.sign(payload.encode())).decode()

    def post(self, caller: Any, params: dict[str, Any]) -> Any:
        class Authenticator:
            async def authenticate(self, request: Any, *, scope: str) -> Any:
                return caller

        app = create_a2a_application(
            service=A2AService(
                authority=Authority(), router=Router(), config=RuntimeConfig()
            ),
            authenticator=Authenticator(),
            approvals=self.chain,
        )
        return TestClient(app).post(
            "/a2a",
            headers={"Authorization": "Bearer verified"},
            json={
                "jsonrpc": "2.0",
                "id": "confirm",
                "method": "arbora.plan/confirm",
                "params": params,
            },
        )
