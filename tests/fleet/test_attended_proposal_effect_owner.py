"""Served proposals use the real EG effect journal over a synthetic WorkItem port."""

from __future__ import annotations

import asyncio
import copy
import time
from typing import Any

import pytest

from arbora.api.contracts.session import use_session
from arbora.api.invoke import invoke
from arbora.api.invoke.steps import OpError, OpResult
from arbora.api.mcp.caller import caller_from_session
from arbora.api.registry import Surface
from arbora.browser_control.browser_control_client import ControlResultMessage
from arbora.browser_control.browser_control_runtime import dispatch_browser_operation
from arbora.core.settings import runtime_config
from arbora.mcp_server.effect_journal import EgEffectJournal
from arbora.mcp_server.served_invocation import (
    compose_served_invocation,
    refused_fleet_effect,
)
from arbora.run_origin import agent_run_origin_scope, native_call_origin_scope
from arbora.security import request_identity
from arbora.security.actor_context import use_actor
from tests.browser_control.test_browser_control_service import _setup
from tests.browser_control.test_browser_proposals import (
    _registered,
    _request,
    _response,
)
from tests.test_served_operation_api import FakeClient, FakePool

pytestmark = [pytest.mark.asyncio, pytest.mark.spec("ARBORA-FLEET-R015")]


async def _wait_for_messages(sent: list[Any], count: int) -> None:
    async with asyncio.timeout(10):
        while len(sent) < count:
            await asyncio.sleep(0.01)


class EffectWorkItems:
    """Async EG WorkItem test port; the production journal owns all decisions."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.claims = 0

    async def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        key = request["work_item_id"]
        created = key not in self.rows
        if created:
            self.rows[key] = {**copy.deepcopy(request), "status": "ready"}
        return {"created": created, "work_item_id": key}

    async def get(self, *, tenant: str, work_item_id: str) -> Any:
        row = self.rows.get(work_item_id)
        assert row is None or row["context"]["tenant_id"] == tenant
        return copy.deepcopy(row)

    async def claim(self, request: dict[str, Any]) -> dict[str, Any]:
        row = self.rows[request["work_item_id"]]
        if row["status"] != "ready":
            return {"claimed": False}
        self.claims += 1
        row.update(status="leased", worker=request["worker_ref"])
        return {
            "claimed": True,
            "lease_holder_ref": row["worker"],
            "lease_epoch": 1,
            "fencing_token": 1,
        }

    async def cas_metadata(self, **request: Any) -> dict[str, Any]:
        row = self.rows[request["work_item_id"]]
        assert row["status"] in request["expected_status"]
        assert row["metadata"] == request["expected_metadata"]
        assert request["expected_lease"] == {
            "worker_ref": row["worker"],
            "lease_epoch": 1,
            "fencing_token": 1,
        }
        row["metadata"] = copy.deepcopy(request["set_metadata"])
        return {"outcome": "applied"}

    async def commit_result(self, **request: Any) -> dict[str, Any]:
        row = self.rows[request["work_item_id"]]
        assert request["worker_id"] == row["worker"]
        assert request["lease_epoch"] == request["fencing_token"] == 1
        row["status"] = request["outcome"]
        return {"status": row["status"]}


class EffectAudit:
    """Audit test port retains the request-linked reservation across interleavings."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.reservations: dict[str, int] = {}

    async def audit_append(self, **fields: Any) -> dict[str, Any]:
        seq = len(self.entries)
        self.entries.append(fields)
        request = fields["request_id"]
        reserved = fields["status"] == "reserved"
        replayed = request in self.reservations
        if reserved:
            self.reservations.setdefault(request, seq)
        return {
            "graph": "tenant:one",
            "seq": self.reservations[request] if reserved else seq,
            "reservation_seq": self.reservations[request],
            "outcome_seq": None if reserved else seq,
            "replayed": replayed,
            "entry_sha256": "0" * 64,
        }


class EffectClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.effect_items = EffectWorkItems()
        self.work_items = self.effect_items  # type: ignore[assignment]
        self.admin = EffectAudit()  # type: ignore[assignment]


def _composed_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, EffectClient, Any]:
    monkeypatch.setattr(runtime_config, "auth_jwt_audience", "arbora-fixture")
    monkeypatch.setattr(runtime_config, "kg_policy_version", "test-1")
    session = request_identity.mint_graph_session(
        request_identity.actor_from_claims(
            {
                "sub": "browser-user",
                "email": "synthetic@example.invalid",
                "tenant_id": "tenant:one",
                "scope": "kg:read kg:write",
                "roles": ["user"],
                "exp": int(time.time()) + 600,
            }
        )
    )
    client = EffectClient()
    invocation = compose_served_invocation(
        pool=FakePool(client),
        graph=session.graph,
        process_session=lambda: session,
        bindings={"browser_control": dispatch_browser_operation},
        fleet_effect=refused_fleet_effect,
    )
    return session, client, invocation.services


@pytest.mark.parametrize("cancel", [False, True])
async def test_served_proposal_replay_and_cancellation_do_not_redispatch(
    monkeypatch: pytest.MonkeyPatch, cancel: bool
) -> None:
    session, client, services = _composed_authority(monkeypatch)
    assert isinstance(services.effects, EgEffectJournal)
    assert "browser.request_call" in services.external_effect_operations
    with use_actor(session.actor), use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        caller = caller_from_session(
            session, credential_kind="bearer", request_id="proposal-test"
        )
        payload = {"payload": _request(session, tool).model_dump(mode="json")}

        async def call() -> Any:
            with agent_run_origin_scope(session, "webui-assistant", "run-effect"):
                with native_call_origin_scope("run-effect"):
                    return await invoke(
                        "browser.request_call",
                        payload,
                        caller,
                        Surface.MCP,
                        services=services,
                        idempotency_key="proposal-one",
                    )

        with _registered(service):
            task = asyncio.create_task(call())
            await _wait_for_messages(sent, 1)
            concurrent = await call()
            assert (
                isinstance(concurrent, OpError) and concurrent.code == "INDETERMINATE"
            )
            assert len(sent) == 1 and client.effect_items.claims == 1
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                await connection.receive(_response(sent[0]))
                await _wait_for_messages(sent, 2)
                await connection.receive(
                    ControlResultMessage(
                        call_id=sent[1].call_id, status="succeeded", result={"ok": True}
                    )
                )
                result = await task
                assert (
                    isinstance(result, OpResult)
                    and result.value["value"]["status"] == "succeeded"
                )
            message_count = len(sent)
            replay = await call()
            assert len(sent) == message_count and client.effect_items.claims == 1
            assert engine._work_item_engine.claim_count == (0 if cancel else 1)
            if cancel:
                assert isinstance(replay, OpError) and replay.code == "INDETERMINATE"
            else:
                assert isinstance(replay, OpResult) and replay.value == result.value
            await connection.disconnect("test_finished")
