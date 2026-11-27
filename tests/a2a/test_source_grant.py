"""The source grant a native run carries comes from the verified caller (ARBORA-OPS-R894)."""

from __future__ import annotations

import inspect
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


def _served(monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]) -> list[Any]:
    """The engine ``main`` shape of ``ListPathGrants``, structurally, with ``answer``."""
    from medulla.generated import METHOD_IDS, models

    sent: list[Any] = []

    async def fake_send(context: Any, method: str, params: dict[str, Any], **_: Any):
        sent.append((method, params))
        return answer

    monkeypatch.setattr("arbora.pin_lag.medulla.engine_send", fake_send)
    monkeypatch.setattr(models, "ListPathGrantsRequest", object, raising=False)
    monkeypatch.setattr(models, "PathGrantList", object, raising=False)
    monkeypatch.setattr("medulla.generated.METHOD_IDS", {*METHOD_IDS, "ListPathGrants"})
    return sent


def _path_grant(
    prefix: str, *, write: bool = True, repo: str = "repo-a"
) -> dict[str, Any]:
    return {
        "repo": repo,
        "principal": "user:1",
        "prefix": prefix,
        "read": True,
        "write": write,
    }


@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.asyncio
async def test_resolution_lists_the_path_grants_of_the_verified_principal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {
        "grants": [
            _path_grant("src"),
            _path_grant("docs"),
            _path_grant("src/secret", write=False),
            _path_grant(""),
        ]
    }
    sent = _served(monkeypatch, answer)
    caller = SimpleNamespace(principal="user:1")
    composed = SimpleNamespace(
        caller=caller,
        services={"source_grants": grants.EngineSourceGrants("feature")},
    )
    assert await grants.resolve_verified_grant(composed) == {
        "repository": "repo-a",
        "branch": "feature",
        "prefixes": ["docs", "src"],
    }
    assert sent == [("ListPathGrants", {"request": {"principal": "user:1"}})]
    # A host that composes no grant service grants nothing, and no engine call is made.
    bare = SimpleNamespace(caller=caller, services={})
    assert await grants.resolve_verified_grant(bare) is None
    assert len(sent) == 1
    # Read-only grants, stored denies and an empty answer are no grant.
    for none in (
        {"grants": [_path_grant("src", write=False)]},
        {"grants": []},
        {},
    ):
        answer.clear()
        answer.update(none)
        assert await grants.resolve_verified_grant(composed) is None
    # Write grants on two repositories make the target ambiguous: no grant.
    answer.clear()
    answer["grants"] = [_path_grant("src"), _path_grant("src", repo="repo-b")]
    assert await grants.resolve_verified_grant(composed) is None


@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.asyncio
async def test_the_pinned_release_grants_nothing_and_names_the_missing_method() -> None:
    from arbora.api.invoke.pipeline import OperationRefused
    from arbora.pin_lag.medulla import send_main

    caller = SimpleNamespace(principal="user:1")
    composed = SimpleNamespace(
        caller=caller,
        services={"source_grants": grants.EngineSourceGrants("feature")},
    )
    # The pinned release lacks the method: the run carries no grant and its changeset
    # ports refuse closed; nothing reads a process copy or a key-value entry.
    assert await grants.resolve_verified_grant(composed) is None
    with pytest.raises(OperationRefused) as refused:
        await send_main(
            composed,
            "ListPathGrants",
            {"request": {"principal": "user:1"}},
            models=grants.LIST_PATH_GRANTS_MODELS,
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "ListPathGrants"


@pytest.mark.spec("ARBORA-OPS-R894")
def test_no_key_value_grant_path_remains() -> None:
    assert not hasattr(grants, "NAMESPACE")
    source = inspect.getsource(grants)
    assert "KvGet" not in source and 'source_grants"' not in source.split("service")[0]


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
        operation_services=None,
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
