"""Served full-chain approval over a real engine (ARBORA-A2A-R005.2.5).

``POST /a2a`` ``arbora.plan/confirm`` -> ``confirm_dispatch`` -> ``HumanGrantStore`` over
the engine's control leases -> the effect, once. The grants live in the real engine
binary the heavy-boot fixture starts, so a second chain composed from nothing (the
restart) still finds, verifies and consumes the grant the first one issued.
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import Mapping
from typing import Any, cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.approval import PendingCallBinding
from arbora.a2a.confirm import (
    ApprovalChain,
    ApprovalRefused,
    RegisteredKeyProducer,
    confirm_dispatch,
    emit_input_required,
    grant_id_for,
)
from arbora.a2a.grant_store import HumanGrantStore
from arbora.a2a.service import A2AService
from arbora.a2a.tool_action_admission import ToolActionAdmitter
from arbora.api.invoke.plan import PlanBinding
from arbora.core.claims import params_digest
from arbora.identity.approval import RegisteredApprovalKey
from tests.a2a.test_application import Authority, Router
from tests.api._support import make_verified_caller
from tests.test_real_boot import (  # noqa: F401
    HEAVY_BOOT_TEST_TIMEOUT_S,
    POLICY_VERSION,
    PROCESS_SUBJECT,
    ROLES,
    TENANT,
    stack,
)

pytestmark = [
    pytest.mark.real_boot,
    pytest.mark.spec("ARBORA-A2A-R005.2.5"),
    pytest.mark.spec("ARBORA-A2A-R005.5"),
    pytest.mark.timeout(HEAVY_BOOT_TEST_TIMEOUT_S),
]

LEASE_KIND = "arbora.human_grant"
HUMAN = "person:approver"
OTHER = "person:other"
human = make_verified_caller("kg:write")


class AsyncLeases:
    """The served engine's control leases as the store's async port."""

    def __init__(self, leases: Any) -> None:
        self._leases = leases

    def __getattr__(self, name: str) -> Any:
        method = getattr(self._leases, name)

        async def call(**fields: Any) -> Any:
            return await asyncio.to_thread(method, **fields)

        return call


class Keys:
    """The identity authority's registered keys: one owner each, no key from the wire."""

    def __init__(self) -> None:
        self.private: dict[str, Ed25519PrivateKey] = {}

    def register(self, who: str) -> Ed25519PrivateKey:
        self.private[who] = Ed25519PrivateKey.generate()
        return self.private[who]

    async def resolve_key(
        self, key_id: str, *, principal_id: str, purpose: str
    ) -> RegisteredApprovalKey:
        private = self.private[principal_id]
        public = private.public_key().public_bytes_raw().hex()
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


class Effects:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def __call__(self, binding: PendingCallBinding) -> Mapping[str, Any]:
        self.ran.append(binding.pending_call_id)
        return {"done": binding.plan.op_id}


class Audit:
    def __init__(self) -> None:
        self.entries: list[Mapping[str, Any]] = []

    async def __call__(self, entry: Mapping[str, Any]) -> None:
        self.entries.append(entry)


class AdmittingDecider:
    """The engine's ``tool_action_admission`` answer, admitting every action."""

    async def decide(self, request: Any, *, tenant: str) -> dict[str, str]:
        return {
            "verdict": "admit",
            "approval_ref": "approval:1",
            "decision_ref": "decision:1",
        }


async def _no_audit(entry: Any) -> None:
    return None


def _chain(
    stack: dict[str, Any],  # noqa: F811
    keys: Keys,
    effects: Effects,
    audit: Audit,  # noqa: F811
) -> ApprovalChain:
    """A chain composed from nothing: every restart builds this again."""
    from medulla import MedullaClient

    from arbora.epistemic import ConnectClient, EpistemicClientPool
    from arbora.mcp_server.served_engine import ServedEngine

    engine = ServedEngine(
        EpistemicClientPool(
            cast(ConnectClient, MedullaClient.connect),
            socket_path=stack["socket_path"],
            auth_secret=stack["secret"],
        )
    )
    store = HumanGrantStore(
        type("Client", (), {"control_leases": AsyncLeases(engine.control_leases)})(),
        lease_kind=LEASE_KIND,
    )
    return ApprovalChain(
        store,
        RegisteredKeyProducer(keys),
        effects,
        audit,
        tool_actions=ToolActionAdmitter(AdmittingDecider(), _no_audit),
    )


def _session() -> Any:
    from arbora.security.actor_context import ActorContext, ActorType, CredentialLease
    from arbora.security.request_identity import _resolve_authenticated_scopes as scopes
    from arbora.security.session import GraphSession

    actor = ActorContext(
        actor_id=PROCESS_SUBJECT,
        actor_type=ActorType.SYSTEM,
        roles=ROLES,
        tenant_id=TENANT,
        authenticated=True,
        credential_lease=CredentialLease(int(time.time()) + 600),
    )
    return GraphSession(
        actor=actor,
        tenant=TENANT,
        scopes=scopes(actor),
        graph="__control__",
        audience="arbora-boot-test",
        policy_version=POLICY_VERSION,
    )


def _plan(principal: str, op: str = "tool.write") -> PlanBinding:
    return PlanBinding(
        op,
        params_digest({"value": 1}),
        principal,
        TENANT,
        POLICY_VERSION,
        params_digest({"registry": 1}),
        "write",
        "plan",
    )


def _sign(key: Ed25519PrivateKey, binding_text: str) -> str:
    return base64.urlsafe_b64encode(key.sign(binding_text.encode())).decode()


def _body(task: str, call: str, plan_ref: str, signature: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": "confirm",
        "method": "arbora.plan/confirm",
        "params": {
            "task_id": task,
            "pending_call_id": call,
            "plan_ref": plan_ref,
            "signature": signature,
            "confirmation_kind": "tool_action",
            "params_digest": _plan(HUMAN).params_digest,
            "trust_mark_ref": "trust:mark",
            "action_class": "egress",
        },
    }


class Authenticator:
    def __init__(self, caller: Any) -> None:
        self.caller = caller

    async def authenticate(self, request: Any, *, scope: str) -> Any:
        return self.caller


def _post(
    chain: ApprovalChain, caller: Any, task: str, call: str, ref: str, sig: str
) -> Any:
    app = create_a2a_application(
        service=A2AService(authority=Authority(), router=Router()),
        authenticator=Authenticator(caller),
        approvals=chain,
    )
    return TestClient(app).post(
        "/a2a",
        headers={"Authorization": "Bearer verified"},
        json=_body(task, call, ref, sig),
    )


def test_request_sign_approve_execute_once_across_restart(
    stack: dict[str, Any],  # noqa: F811
) -> None:
    from arbora.security.session import use_session

    keys, effects, audit = Keys(), Effects(), Audit()
    approver_key = keys.register(HUMAN)
    keys.register(OTHER)
    approver = human(principal=HUMAN, tenant=TENANT, policy_revision=POLICY_VERSION)
    task, call, ref = "task:chain", "call:one", "arbora_plan:chain"

    with use_session(_session()):
        # Request: the pending effect becomes durable and the task reads input-required.
        first = _chain(stack, keys, effects, audit)
        projection = asyncio.run(
            emit_input_required(
                first,
                task_id=task,
                parent_work_item_id="work:parent",
                child_work_item_id="work:child",
                pending_call_id=call,
                plan_ref=ref,
                plan=_plan(HUMAN),
            )
        )
        assert projection["state"] == "input-required"
        pending = projection["pending_call"]
        assert pending["grant_id"] == grant_id_for(task, call)
        good = _sign(approver_key, pending["signing_payload"])

        # Restart: nothing carried over but the engine. Negative cases refuse first.
        restarted = _chain(stack, keys, effects, audit)
        foreign = human(principal=OTHER, tenant=TENANT, policy_revision=POLICY_VERSION)
        assert _post(restarted, foreign, task, call, ref, good).status_code == 403
        wrong_tenant = human(
            principal=HUMAN, tenant="other", policy_revision=POLICY_VERSION
        )
        assert _post(restarted, wrong_tenant, task, call, ref, good).status_code == 403
        forged = _sign(keys.private[OTHER], pending["signing_payload"])
        assert _post(restarted, approver, task, call, ref, forged).status_code == 403
        changed = _post(restarted, approver, task, call, "arbora_plan:other", good)
        assert changed.status_code == 403
        assert effects.ran == []

        # Approve: concurrent replays of the one valid signature run the effect once.
        async def race() -> list[Any]:
            return await asyncio.gather(
                *(
                    confirm_dispatch(
                        _body(task, call, ref, good)["params"],
                        caller=approver,
                        chain=_chain(stack, keys, effects, audit),
                    )
                    for _ in range(4)
                ),
                return_exceptions=True,
            )

        outcomes = asyncio.run(race())
        approved = [o for o in outcomes if isinstance(o, dict)]
        refused = [o for o in outcomes if isinstance(o, ApprovalRefused)]
        assert len(approved) == 1 and len(refused) == 3, outcomes
        assert {r.code for r in refused} <= {"GRANT_REPLAYED", "GRANT_REVOKED"}
        assert effects.ran == [call]
        # A later replay over the wire also refuses, and the effect stays at one.
        replay = _post(restarted, approver, task, call, ref, good)
        assert replay.status_code == 403
        assert replay.json()["error"]["message"] == "GRANT_REPLAYED"
        assert effects.ran == [call]

    consumed = [e for e in audit.entries if e["outcome"] == "consumed"]
    assert len(consumed) == 1 and consumed[0]["grant_id"] == grant_id_for(task, call)
    # Every attempt left one audit entry carrying the grant id and its outcome.
    assert all("grant_id" in e and "outcome" in e for e in audit.entries)
    assert len(audit.entries) >= 8
