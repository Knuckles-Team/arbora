"""The source grant a native run carries comes from the verified caller (ARBORA-OPS-R894)."""

from __future__ import annotations

import json
from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.a2a import source_grant as grants
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.models import A2AMessage, A2ARouteDecision, A2ATextPart
from arbora.agent_host.changeset_seams import source_target

GRANT = {"repository": "repo-a", "branch": "feature", "prefixes": ["src"]}


def _message(metadata: dict[str, Any] | None = None) -> A2AMessage:
    return A2AMessage(
        role="user",
        parts=[A2ATextPart(text="do work")],
        message_id="message-1",
        metadata=metadata or {},
    )


def _metadata(message: A2AMessage) -> dict[str, Any]:
    return WorkItemA2AAuthority._metadata(
        message=message,
        context_id="ctx",
        owner_ref="owner",
        request_digest=WorkItemA2AAuthority._request_digest(
            message,
            "ctx",
            A2ARouteDecision(agent_name="expert", selection_mode="agent-assemble"),
        ),
        decision=A2ARouteDecision(agent_name="expert", selection_mode="agent-assemble"),
    )


@pytest.mark.spec("ARBORA-OPS-R894")
def test_a_run_with_the_verified_grant_carries_it_and_may_submit() -> None:
    with grants.source_grant_scope(GRANT):
        metadata = _metadata(_message())
    assert metadata[grants.SOURCE_GRANT_KEY] == GRANT
    target = source_target({"metadata": metadata})
    assert target is not None and target.branch == "feature"


@pytest.mark.spec("ARBORA-OPS-R894")
def test_a_run_without_a_grant_carries_none_and_refuses_changeset_submission() -> None:
    metadata = _metadata(_message())
    assert grants.SOURCE_GRANT_KEY not in metadata
    assert source_target({"metadata": metadata}) is None
    with grants.source_grant_scope({**GRANT, "prefixes": []}):
        assert grants.SOURCE_GRANT_KEY not in _metadata(_message())


@pytest.mark.spec("ARBORA-OPS-R894")
def test_request_text_never_sets_the_grant() -> None:
    forged = _message({grants.SOURCE_GRANT_KEY: {**GRANT, "repository": "other"}})
    metadata = _metadata(forged)
    assert grants.SOURCE_GRANT_KEY not in metadata
    with grants.source_grant_scope(GRANT):
        assert _metadata(forged)[grants.SOURCE_GRANT_KEY] == GRANT


@pytest.mark.spec("ARBORA-OPS-R894")
def test_the_grant_is_part_of_the_request_identity() -> None:
    decision = A2ARouteDecision(agent_name="expert", selection_mode="agent-assemble")
    message = _message()
    plain = WorkItemA2AAuthority._request_digest(message, "ctx", decision)
    with grants.source_grant_scope(GRANT):
        granted = WorkItemA2AAuthority._request_digest(message, "ctx", decision)
    assert plain != granted
    assert grants.bound_source_grant() is None


@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.asyncio
async def test_resolution_reads_the_engine_entry_of_the_verified_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []
    entry: dict[str, Any] = {"value": json.dumps(GRANT).encode(), "version": 1}

    async def fake_send(context: Any, method: str, params: dict[str, Any], **_: Any):
        sent.append((method, params))
        return entry

    monkeypatch.setattr("arbora.api.ops._engine.engine_send", fake_send)
    caller = SimpleNamespace(principal="user:1")
    composed = SimpleNamespace(
        caller=caller, services={"source_grants": grants.EngineSourceGrants()}
    )
    assert await grants.resolve_verified_grant(composed) == GRANT
    assert sent == [("KvGet", {"namespace": "source_grants", "key": "user:1"})]
    # A host that composes no grant service grants nothing, and no engine call is made.
    bare = SimpleNamespace(caller=caller, services={})
    assert await grants.resolve_verified_grant(bare) is None
    assert len(sent) == 1
    # A malformed or absent entry is no grant.
    entry["value"] = b"{not json"
    assert await grants.resolve_verified_grant(composed) is None
    entry.clear()
    assert await grants.resolve_verified_grant(composed) is None


@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.asyncio
async def test_agents_run_binds_the_resolved_grant_for_the_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops import agents

    seen: list[Any] = []

    class _A2A:
        async def send_message(self, **_: Any) -> Any:
            seen.append(grants.bound_source_grant())
            return SimpleNamespace(model_dump=lambda **_kw: {"id": "task"})

    class _Source:
        async def resolve(self, context: Any) -> Any:
            return GRANT

    run = next(op for op in agents.operations() if op.id == "agents.run")
    context = SimpleNamespace(
        caller=SimpleNamespace(session=object(), principal="user:1"),
        idempotency_key="key",
        services={"a2a": _A2A(), "source_grants": _Source()},
    )
    monkeypatch.setattr(
        "arbora.security.session.use_session", lambda _session: nullcontext()
    )
    await agents.handle_agent(
        context,
        {"message": {"kind": "message", "metadata": {"source_grant": {"x": 1}}}},
        run,
    )
    assert seen == [GRANT]
    assert grants.bound_source_grant() is None
