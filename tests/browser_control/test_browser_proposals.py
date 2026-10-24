"""Attended proposals traverse the real browser service and execution fences."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from arbora.api.contracts.session import GraphSession, use_session
from arbora.browser_control.browser_control_api import BrowserProposalRequest
from arbora.browser_control.browser_control_client import (
    ControlConfirmMessage,
    ControlProposalResponseMessage,
    ControlResultMessage,
)
from arbora.browser_control.browser_control_runtime import (
    bind_browser_control_owner,
    dispatch_browser_control,
    register_browser_control_service,
    unregister_browser_control_service,
)
from arbora.run_origin import (
    agent_run_origin_scope,
    native_call_origin_scope,
    require_native_call_origin,
)
from tests.browser_control.test_browser_control_service import (
    _binding,
    _setup,
    _wait_for_messages,
)
from tests.browser_control.test_browser_control_service import session as session

pytestmark = [pytest.mark.asyncio, pytest.mark.spec("ARBORA-FLEET-R015")]


def _request(session: GraphSession, tool: Any) -> BrowserProposalRequest:
    return BrowserProposalRequest(
        document_ref=_binding(session).document_ref,
        registration_generation=1,
        tool_id=tool.tool_id,
        schema_digest=tool.schema_digest,
        arguments={"value": "safe"},
        timeout_seconds=15,
    )


async def _propose(
    service: Any,
    request: BrowserProposalRequest,
    session: GraphSession,
    origins: list[Any] | None = None,
    *,
    routed: bool = False,
) -> Any:
    with agent_run_origin_scope(session, "web", "test-run"):
        with native_call_origin_scope("test-run"):
            if origins is not None:
                origins.append(require_native_call_origin())
            if routed:
                return await dispatch_browser_control(
                    "request_call", request.model_dump(mode="json")
                )
            return await service.request_call(request)


def _response(
    prompt: Any, accepted: bool = True, **changes: Any
) -> ControlProposalResponseMessage:
    return ControlProposalResponseMessage.model_validate(
        {
            "proposal_id": prompt.proposal_id,
            "proposal_digest": prompt.proposal_digest,
            "accepted": accepted,
            **changes,
        }
    )


def _leases(engine: Any) -> list[Any]:
    return [
        row
        for row in engine.browser_control_state.nodes.values()
        if row.get("node_type") == "BrowserControlLease"
    ]


@contextmanager
def _registered(service: Any) -> Iterator[None]:
    register_browser_control_service(service)
    bind_browser_control_owner(service)
    try:
        yield
    finally:
        unregister_browser_control_service(service)


async def test_proposal_refuses_missing_native_origin(session: GraphSession) -> None:
    with use_session(session):
        service, _connection, engine, sent, tool = await _setup(session)
        with pytest.raises(PermissionError, match="origin"):
            await service.request_call(_request(session, tool))
        assert not sent and not _leases(engine)
        assert engine.browser_control_state.claim_count == 0


@pytest.mark.parametrize("routed", [False, True])
async def test_read_executes_only_after_human_acceptance(
    session: GraphSession, routed: bool
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        with _registered(service):
            task = asyncio.create_task(
                _propose(service, _request(session, tool), session, routed=routed)
            )
            await _wait_for_messages(sent, 1)
            prompt = sent[0]
            assert prompt.type == "control.proposal_request"
            assert prompt.requester_agent == "web" and prompt.requester_run_ref
            assert not _leases(engine)
            assert engine.browser_control_state.claim_count == 0
            await connection.receive(_response(prompt))
            await _wait_for_messages(sent, 2)
            call = sent[1]
            assert call.type == "control.call" and call.authorization == "read"
            assert _leases(engine) and engine.browser_control_state.claim_count == 1
            await connection.receive(
                ControlResultMessage(
                    call_id=call.call_id, status="succeeded", result={"ok": True}
                )
            )
            receipt = await asyncio.wait_for(task, 2)
            assert receipt.status == "succeeded" and receipt.result == {"ok": True}
            assert not service._proposals
            traces = [
                mutation["properties"]
                for batch in engine.trace_batches
                for mutation in batch
                if mutation["kind"] == "node"
            ]
            assert any(
                row.get("requester_agent") == "web"
                and row.get("requester_run_reference") == prompt.requester_run_ref
                and row.get("proposal_digest") == prompt.proposal_digest
                for row in traces
            )
            with pytest.raises(PermissionError):
                await connection.receive(_response(prompt))
            assert engine.browser_control_state.claim_count == 1


async def test_mutation_requires_separate_exact_confirmation(
    session: GraphSession,
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session, mutation=True)
        task = asyncio.create_task(_propose(service, _request(session, tool), session))
        await _wait_for_messages(sent, 1)
        proposal = sent[0]
        await connection.receive(_response(proposal))
        await _wait_for_messages(sent, 2)
        confirmation = sent[1]
        assert confirmation.type == "control.confirmation_request"
        assert engine.browser_control_state.claim_count == 0
        with pytest.raises(PermissionError):
            await connection.receive(
                ControlConfirmMessage(
                    call_id=confirmation.call_id,
                    confirmation_digest=proposal.proposal_digest,
                )
            )
        assert not any(message.type == "control.call" for message in sent)
        await connection.receive(
            ControlConfirmMessage(
                call_id=confirmation.call_id,
                confirmation_digest=confirmation.confirmation_digest,
            )
        )
        await _wait_for_messages(sent, 3)
        call = sent[2]
        assert (
            call.type == "control.call" and call.authorization == "confirmed_mutation"
        )
        await connection.receive(
            ControlResultMessage(
                call_id=call.call_id, status="succeeded", result={"ok": True}
            )
        )
        assert (await asyncio.wait_for(task, 2)).status == "succeeded"


async def test_denial_and_wrong_digest_cannot_issue_lease(
    session: GraphSession,
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        task = asyncio.create_task(_propose(service, _request(session, tool), session))
        await _wait_for_messages(sent, 1)
        prompt = sent[0]
        with pytest.raises(PermissionError):
            await connection.receive(
                _response(prompt, proposal_digest="sha256:" + "0" * 64)
            )
        assert not _leases(engine) and not task.done()
        await connection.receive(_response(prompt, accepted=False))
        with pytest.raises(PermissionError, match="declined"):
            await asyncio.wait_for(task, 2)
        assert not _leases(engine) and engine.browser_control_state.claim_count == 0
        assert not service._proposals


@pytest.mark.parametrize("reason", ["origin", "disconnect", "caller"])
async def test_pending_proposal_is_withdrawn_on_authority_loss(
    session: GraphSession, reason: str
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        origins: list[Any] = []
        task = asyncio.create_task(
            _propose(service, _request(session, tool), session, origins)
        )
        await _wait_for_messages(sent, 1)
        prompt = sent[0]
        if reason == "origin":
            origins[0].close()
        elif reason == "disconnect":
            await connection.disconnect("test_disconnect")
        else:
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not _leases(engine) and engine.browser_control_state.claim_count == 0
        assert not service._proposals
        with pytest.raises((PermissionError, RuntimeError)):
            await connection.receive(_response(prompt))


async def test_accepted_mutation_is_revoked_before_confirmation(
    session: GraphSession,
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session, mutation=True)
        origins: list[Any] = []
        task = asyncio.create_task(
            _propose(service, _request(session, tool), session, origins)
        )
        await _wait_for_messages(sent, 1)
        prompt = sent[0]
        await connection.receive(_response(prompt))
        await _wait_for_messages(sent, 2)
        assert sent[1].type == "control.confirmation_request"
        with pytest.raises(PermissionError):
            await connection.receive(_response(prompt))
        origins[0].close()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert not any(message.type == "control.call" for message in sent)
        assert engine.browser_control_state.claim_count == 0
        assert all(row["status"] == "revoked" for row in _leases(engine))
        assert not service._proposals


async def test_caller_cancellation_during_cleanup_finishes_lease_revocation(
    session: GraphSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        entered = asyncio.Event()
        release = asyncio.Event()
        revoked = asyncio.Event()
        original = service.revoke_lease

        async def delayed(request: Any) -> Any:
            entered.set()
            await release.wait()
            result = await original(request)
            revoked.set()
            return result

        monkeypatch.setattr(service, "revoke_lease", delayed)
        task = asyncio.create_task(_propose(service, _request(session, tool), session))
        await _wait_for_messages(sent, 1)
        await connection.receive(_response(sent[0]))
        await _wait_for_messages(sent, 2)
        call = sent[1]
        assert call.type == "control.call"
        await connection.receive(
            ControlResultMessage(
                call_id=call.call_id, status="succeeded", result={"ok": True}
            )
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        # Let cancellation arrive at the blocked cleanup before releasing it.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        await asyncio.wait_for(revoked.wait(), 1)
        assert all(row["status"] == "revoked" for row in _leases(engine))
        assert not service._proposals


async def test_cancellation_during_lease_issuance_recovers_and_revokes_receipt(
    session: GraphSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        committed = asyncio.Event()
        release = asyncio.Event()
        original = service.issue_lease

        async def delayed(request: Any) -> Any:
            receipt = await original(request)
            committed.set()
            await release.wait()
            return receipt

        monkeypatch.setattr(service, "issue_lease", delayed)
        task = asyncio.create_task(_propose(service, _request(session, tool), session))
        await _wait_for_messages(sent, 1)
        await connection.receive(_response(sent[0]))
        await asyncio.wait_for(committed.wait(), 2)
        assert _leases(engine)
        task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert all(row["status"] == "revoked" for row in _leases(engine))
        assert not any(message.type == "control.call" for message in sent)
        assert engine.browser_control_state.claim_count == 0


async def test_proposal_rejects_oversized_arguments_and_caller_provenance(
    session: GraphSession,
) -> None:
    from pydantic import ValidationError

    with use_session(session):
        service, _connection, engine, sent, tool = await _setup(session)
        payload = _request(session, tool).model_dump(mode="json")
        with pytest.raises(ValidationError):
            BrowserProposalRequest.model_validate(
                {**payload, "arguments": {"value": "x" * 8192}}
            )
        with pytest.raises(ValidationError):
            BrowserProposalRequest.model_validate(
                {**payload, "requester_agent": "forged"}
            )
        assert not sent and not _leases(engine) and not service._proposals


@pytest.mark.parametrize(
    "attributes",
    [
        {"requester_agent": "a" * 513},
        {"requester_run_reference": "raw-untrusted-run"},
        {"proposal_digest": "not-a-digest"},
        {"proposal_reference": "invalid proposal id"},
    ],
)
async def test_invalid_proposal_audit_attributes_never_reach_storage(
    session: GraphSession, attributes: dict[str, Any]
) -> None:
    from arbora.browser_control.trace_bundle import record_trace_bundle
    from tests.browser_control.test_browser_control_service import _Engine

    engine = _Engine()
    with use_session(session), pytest.raises(ValueError):
        record_trace_bundle(
            engine,
            session=session,
            run_id="test-run",
            agent_name="browser-control",
            tool_name="agent-webui.get-page-context",
            arguments={},
            result=None,
            error=None,
            status="pending",
            attributes=attributes,
        )
    assert not engine.trace_batches


@pytest.mark.parametrize("boundary", ["owner", "tenant"])
async def test_foreign_authority_cannot_propose_or_accept(
    session: GraphSession, boundary: str
) -> None:
    from dataclasses import replace

    actor = replace(
        session.actor,
        actor_id="foreign-owner" if boundary == "owner" else session.actor.actor_id,
        tenant_id="foreign-tenant" if boundary == "tenant" else session.tenant,
    )
    foreign = replace(session, actor=actor, tenant=actor.tenant_id)
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        request = _request(session, tool)
        with use_session(foreign), pytest.raises(PermissionError):
            await _propose(service, request, foreign)
        assert not sent and not _leases(engine)
        task = asyncio.create_task(_propose(service, request, session))
        await _wait_for_messages(sent, 1)
        with use_session(foreign), pytest.raises(PermissionError):
            await connection.receive(_response(sent[0]))
        assert not task.done() and not _leases(engine)
        assert not next(iter(service._proposals.values())).accepted
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert engine.browser_control_state.claim_count == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"registration_generation": 2},
        {"schema_digest": "sha256:" + "0" * 64},
    ],
)
async def test_stale_proposal_registration_is_refused_before_prompt(
    session: GraphSession, changes: dict[str, Any]
) -> None:
    with use_session(session):
        service, _connection, engine, sent, tool = await _setup(session)
        request = _request(session, tool).model_copy(update=changes)
        with pytest.raises(PermissionError, match="registration changed"):
            await _propose(service, request, session)
        assert not sent and not _leases(engine) and not service._proposals
        assert engine.browser_control_state.claim_count == 0


async def test_unanswered_proposal_times_out_and_rejects_late_acceptance(
    session: GraphSession,
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        request = _request(session, tool).model_copy(update={"timeout_seconds": 0.05})
        task = asyncio.create_task(_propose(service, request, session))
        await _wait_for_messages(sent, 1)
        prompt = sent[0]
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(task, 2)
        assert not service._proposals and not _leases(engine)
        assert any(message.type == "control.proposal_withdrawn" for message in sent)
        with pytest.raises(PermissionError):
            await connection.receive(_response(prompt))
        assert engine.browser_control_state.claim_count == 0


async def test_one_pending_proposal_per_document_preserves_first_request(
    session: GraphSession,
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        request = _request(session, tool)
        first = asyncio.create_task(_propose(service, request, session))
        await _wait_for_messages(sent, 1)
        prompt = sent[0]
        with pytest.raises(RuntimeError, match="already has a pending proposal"):
            await _propose(service, request, session)
        assert len(sent) == 1 and not first.done()
        assert list(service._proposals) == [prompt.proposal_id]
        assert not _leases(engine) and engine.browser_control_state.claim_count == 0
        await connection.receive(_response(prompt, accepted=False))
        with pytest.raises(PermissionError, match="declined"):
            await asyncio.wait_for(first, 2)
        assert not service._proposals


async def test_expired_authority_during_cleanup_does_not_join_own_parent(
    session: GraphSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        original = service.revoke_lease

        async def expired_cleanup(request: Any) -> Any:
            now = service._clock()
            monkeypatch.setattr(service, "_clock", lambda: now + 1000)
            return await original(request)

        monkeypatch.setattr(service, "revoke_lease", expired_cleanup)
        task = asyncio.create_task(_propose(service, _request(session, tool), session))
        await _wait_for_messages(sent, 1)
        pending = service._proposals[sent[0].proposal_id]
        await connection.receive(_response(sent[0]))
        await _wait_for_messages(sent, 2)
        call = sent[1]
        assert call.type == "control.call"
        await connection.receive(
            ControlResultMessage(
                call_id=call.call_id, status="succeeded", result={"ok": True}
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert pending.task is not None and pending.task.done()
        assert not service._proposals and not service._channels
        assert all(row["status"] == "expired" for row in _leases(engine))


async def test_failed_authority_during_lease_issue_does_not_join_own_parent(
    session: GraphSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    with use_session(session):
        service, connection, engine, sent, tool = await _setup(session)
        original = service.issue_lease

        async def revoked_session(_binding: Any) -> bool:
            return False

        async def revoked_issue(request: Any) -> Any:
            monkeypatch.setattr(service, "_session_revalidator", revoked_session)
            return await original(request)

        monkeypatch.setattr(service, "issue_lease", revoked_issue)
        task = asyncio.create_task(_propose(service, _request(session, tool), session))
        await _wait_for_messages(sent, 1)
        pending = service._proposals[sent[0].proposal_id]
        await connection.receive(_response(sent[0]))
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert pending.task is not None and pending.task.done()
        assert not service._proposals and not service._channels and not _leases(engine)
        assert not any(message.type == "control.call" for message in sent)
