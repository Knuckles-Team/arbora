"""Unary A2A transport contracts."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from arbora.a2a.application import create_a2a_application
from arbora.a2a.authority import A2AIdempotencyConflict
from arbora.a2a.service import A2AService
from arbora.api.domain.a2a.models import (
    A2AArtifact,
    A2ARouteDecision,
    A2ATask,
    A2ATaskStatus,
)


class Authenticator:
    def __init__(self) -> None:
        self.scopes: list[str] = []

    async def authenticate(self, request: Any, *, scope: str) -> None:
        self.scopes.append(scope)
        if request.headers.get("Authorization") != "Bearer verified":
            raise HTTPException(status_code=401)


class Router:
    async def route(self, message: Any, *, context_budget_tokens: int | None) -> Any:
        assert message.task_text() == "do work"
        assert context_budget_tokens is None
        return A2ARouteDecision(agent_name="meristem-expert", selection_mode="test")


class Authority:
    def __init__(self) -> None:
        self.task = A2ATask(
            id="a2a-" + "1" * 64,
            context_id="a2a-context-" + "2" * 64,
            status=A2ATaskStatus(state="submitted"),
        )

    async def dispatch(self, **kwargs: Any) -> A2ATask:
        assert kwargs["idempotency_key"] == "send-key"
        return self.task

    async def get(self, task_id: str) -> A2ATask | None:
        return self.task if task_id == self.task.id else None

    async def list(self, **kwargs: Any) -> tuple[list[A2ATask], str | None]:
        return [self.task], "next"

    async def cancel(self, task_id: str) -> A2ATask:
        return self.task.model_copy(update={"status": A2ATaskStatus(state="canceled")})


def _app() -> tuple[Any, Authenticator, Authority]:
    auth = Authenticator()
    authority = Authority()
    app = create_a2a_application(
        service=A2AService(authority=authority, router=Router()),
        authenticator=auth,
    )
    return app, auth, authority


@pytest.mark.spec("ARBORA-OPS-R037")
def test_agent_card_is_authenticated_truthful_and_has_one_path() -> None:
    app, auth, _authority = _app()
    client = TestClient(app)
    assert client.get("/.well-known/agent-card.json").status_code == 401
    response = client.get(
        "/.well-known/agent-card.json",
        headers={"Authorization": "Bearer verified"},
    )
    assert response.status_code == 200
    card = response.json()
    assert card["url"] == "http://testserver/a2a"
    assert card["capabilities"] == {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
        "extensions": [],
    }
    assert card["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    assert client.get("/.well-known/agent.json").status_code == 404
    assert auth.scopes == ["kg:read", "kg:read"]


@pytest.mark.spec("ARBORA-OPS-R037")
def test_json_rpc_unary_methods_use_typed_shared_contract() -> None:
    app, auth, authority = _app()
    client = TestClient(app)
    headers = {
        "Authorization": "Bearer verified",
        "Idempotency-Key": "send-key",
    }
    message = {
        "role": "user",
        "parts": [{"kind": "text", "text": "do work"}],
        "messageId": "message-1",
    }
    calls = (
        ("message/send", {"message": message}),
        ("tasks/get", {"id": authority.task.id}),
        ("tasks/list", {"limit": 10}),
        ("tasks/cancel", {"id": authority.task.id}),
    )
    for method, params in calls:
        response = client.post(
            "/a2a",
            headers=headers,
            json={"jsonrpc": "2.0", "id": method, "method": method, "params": params},
        )
        assert response.status_code == 200
        assert "result" in response.json()
    assert auth.scopes == ["kg:write", "kg:read", "kg:read", "kg:write"]


def test_json_rpc_rejects_missing_idempotency_invalid_shape_and_unknown_task() -> None:
    app, _auth, _authority = _app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer verified"}
    invalid = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {"message": {"role": "user", "parts": []}},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == -32602

    missing = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tasks/get",
            "params": {"id": "a2a-" + "9" * 64},
        },
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == -32001

    unknown = client.post(
        "/a2a",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 3, "method": "tasks/unknown", "params": {}},
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == -32601


@pytest.mark.spec("ARBORA-A2A-R001.1")
def test_json_rpc_refuses_resubscribe_until_streaming_is_implemented() -> None:
    """ARBORA-A2A-R001.1: resubscribe is on the method table but fail-closed.

    Durable streaming has no bounded, restart-safe event-cursor backing yet
    (ARBORA-A2A-R001), so ``tasks/resubscribe`` must refuse distinctly from
    an unrecognized method, and must still validate its params.
    """
    app, auth, authority = _app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer verified"}

    refused = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "resubscribe",
            "method": "tasks/resubscribe",
            "params": {"id": authority.task.id, "afterSeq": 0},
        },
    )
    assert refused.status_code == 501
    assert refused.json()["error"] == {
        "code": -32010,
        "message": "durable task streaming and resubscribe are not available yet",
    }
    assert auth.scopes == ["kg:read"]

    invalid = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "resubscribe-invalid",
            "method": "tasks/resubscribe",
            "params": {},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == -32602


def test_json_rpc_preserves_service_error_translation() -> None:
    class ConflictAuthority(Authority):
        async def dispatch(self, **kwargs: Any) -> A2ATask:
            raise A2AIdempotencyConflict("idempotency conflict")

    app = create_a2a_application(
        service=A2AService(authority=ConflictAuthority(), router=Router()),
        authenticator=Authenticator(),
    )
    response = TestClient(app).post(
        "/a2a",
        headers={
            "Authorization": "Bearer verified",
            "Idempotency-Key": "send-key",
        },
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "do work"}],
                    "messageId": "message-1",
                }
            },
        },
    )
    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": -32009,
        "message": "idempotency conflict",
    }


@pytest.mark.spec("ARBORA-A2A-R006.1")
def test_json_rpc_refuses_op_invoke_until_registry_bridge_exists() -> None:
    """ARBORA-A2A-R006.1: ``arbora.op/invoke`` is on the method table but
    fail-closed until it is bridged to the shared hosted-operation registry
    (``Surface.A2A``), with a distinct error code, not method-not-found.
    """
    app, auth, _authority = _app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer verified"}

    refused = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "invoke",
            "method": "arbora.op/invoke",
            "params": {"op": "kg.search", "params": {}},
        },
    )
    assert refused.status_code == 501
    assert refused.json()["error"]["code"] == -32011
    assert auth.scopes == ["kg:read"]

    invalid = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "invoke-invalid",
            "method": "arbora.op/invoke",
            "params": {},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == -32602


@pytest.mark.spec("ARBORA-A2A-R005.1")
@pytest.mark.spec("ARBORA-A2A-R006.1")
def test_json_rpc_refuses_plan_confirm_until_approval_exchange_exists() -> None:
    """ARBORA-A2A-R005.1/R006.1: the human approval response method is
    registered but every confirmation attempt is rejected until the full
    signed exchange is implemented end to end.
    """
    app, auth, authority = _app()
    client = TestClient(app)
    headers = {"Authorization": "Bearer verified"}

    refused = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "confirm",
            "method": "arbora.plan/confirm",
            "params": {
                "task_id": authority.task.id,
                "pending_call_id": "call-1",
                "plan_ref": "plan-1",
                "signature": "sig",
                "confirmation_kind": "held_item",
                "item_kind": "run_output_proposal",
                "item_ref": "item-1",
                "content_digest": "b" * 64,
                "engine_decision_ref": "dec-1",
            },
        },
    )
    assert refused.status_code == 501
    assert refused.json()["error"]["code"] == -32011
    assert auth.scopes == ["kg:write"]

    invalid = client.post(
        "/a2a",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": "confirm-invalid",
            "method": "arbora.plan/confirm",
            "params": {},
        },
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == -32602


# -- ARBORA-A2A-R022: critique-gated output release over the real route ---------------

_OUTPUT = [
    {
        "artifactId": "reply",
        "parts": [{"kind": "text", "text": "the generated answer"}],
    }
]


def _held(task: A2ATask, **changes: Any) -> dict[str, Any]:
    from arbora.a2a.critique_gate import output_digest

    held: dict[str, Any] = {
        "itemRef": "held:one",
        "contentDigest": output_digest(task),
        "engineDecisionRef": "dec:one",
        "checklist": [{"id": "q1", "question": "Does the answer cite its source?"}],
    }
    held.update(changes)
    return held


class _Engine:
    """A controlled held-item decider recording each question it is asked."""

    def __init__(self, outcome: str = "admitted", reason: str | None = None) -> None:
        self.outcome, self.reason = outcome, reason
        self.asked: list[Any] = []

    async def decide(self, request: Any, *, tenant: str) -> dict[str, Any] | None:
        self.asked.append(request)
        return {
            "outcome": self.outcome,
            "verdict_ref": "verdict:one",
            **({"reason": self.reason} if self.reason else {}),
        }


def _gated_app(
    engine: _Engine, metadata: dict[str, Any] | None, *, tamper: bool = False
) -> tuple[TestClient, list[dict[str, Any]]]:
    from types import SimpleNamespace

    from arbora.api.domain.a2a.held_item_admission import HeldItemAdmitter

    audits: list[dict[str, Any]] = []

    async def audit(entry: Any) -> None:
        audits.append(dict(entry))

    authority = Authority()
    task = authority.task.model_copy(
        update={"artifacts": [A2AArtifact.model_validate(a) for a in _OUTPUT]}
    )
    if metadata is not None:
        held = _held(task, **metadata)
        if tamper:
            task = task.model_copy(
                update={
                    "artifacts": [
                        A2AArtifact.model_validate(
                            {**_OUTPUT[0], "parts": [{"kind": "text", "text": "x"}]}
                        )
                    ]
                }
            )
        task = task.model_copy(update={"metadata": {"heldOutput": held}})
    authority.task = task
    chain = SimpleNamespace(held_items=HeldItemAdmitter(engine, audit))
    app = create_a2a_application(
        service=A2AService(authority=authority, router=Router()),
        authenticator=Authenticator(),
        approvals=chain,  # type: ignore[arg-type]
    )
    return TestClient(app), audits


def _send(client: TestClient) -> Any:
    return client.post(
        "/a2a",
        headers={"Authorization": "Bearer verified", "Idempotency-Key": "send-key"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "message/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"kind": "text", "text": "do work"}],
                    "messageId": "m-1",
                }
            },
        },
    )


@pytest.mark.spec("ARBORA-A2A-R022")
def test_send_message_holds_output_until_bound_critique_passes() -> None:
    engine = _Engine()
    client, audits = _gated_app(engine, {})
    body = _send(client).json()
    # Only an admitted verdict bound to this exact output releases the reply.
    assert body["result"]["artifacts"][0]["parts"][0]["text"] == "the generated answer"
    assert body["result"]["metadata"]["critique"] == {
        "outcome": "admitted",
        "verdictRef": "verdict:one",
    }
    [asked] = engine.asked
    assert asked.item_kind == "run_output_proposal" and asked.item_ref == "held:one"
    assert asked.engine_decision_ref == "dec:one"
    # The one ApprovalChain admitter records its audit entry before and after.
    assert [a["verdict_ref"] for a in audits] == ["", "verdict:one"]


@pytest.mark.spec("ARBORA-A2A-R022")
def test_send_message_rejects_untyped_preflight_and_failed_critique() -> None:
    def refused(response: Any) -> str:
        body = response.json()
        assert "result" not in body, "a held output must send nothing"
        assert body["error"]["code"] == -32011
        return str(body["error"]["data"]["reason"])

    # No typed preflight: nothing is asked of the engine and the output stays held.
    engine = _Engine()
    client, _ = _gated_app(engine, None)
    assert refused(_send(client)) == "POLICY_EVIDENCE_MISSING"
    # An empty checklist is untyped.
    client, _ = _gated_app(engine, {"checklist": []})
    assert refused(_send(client)) == "POLICY_EVIDENCE_MISSING"
    # A verdict bound to a different digest than the output being sent.
    client, _ = _gated_app(engine, {}, tamper=True)
    assert refused(_send(client)) == "DIGEST_MISMATCH"
    assert engine.asked == []
    # The engine rejects, or abstains: the output stays held.
    rejected = _Engine("rejected", "RETIRED")
    client, audits = _gated_app(rejected, {})
    assert refused(_send(client)) == "RETIRED"
    assert audits[-1]["reason"] == "RETIRED"
    client, _ = _gated_app(_Engine("abstained"), {})
    assert refused(_send(client)) == "ABSTAINED"

    # An engine that answers nothing is missing policy evidence, never a release.
    class Silent(_Engine):
        async def decide(self, request: Any, *, tenant: str) -> None:
            return None

    client, _ = _gated_app(Silent(), {})
    assert refused(_send(client)) == "POLICY_EVIDENCE_MISSING"
