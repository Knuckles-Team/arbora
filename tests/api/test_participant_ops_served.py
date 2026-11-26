"""Participant memory operations (ARBORA-OPS-R897.1 to R897.5, R897.8)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.ops import participants
from tests.api._knowledge_support import NO_OP, caller_context
from tests.test_served_operation_api import _call, served  # noqa: F401


def _record(monkeypatch: pytest.MonkeyPatch, answer: Any = None) -> list[Any]:
    sent: list[Any] = []

    async def engine_send(
        context: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> Any:
        sent.append((method, params, idempotent))
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {"ok": True}

    monkeypatch.setattr(participants, "engine_send", engine_send)
    return sent


@pytest.mark.spec("ARBORA-OPS-R897.1")
def test_models_build_the_engine_shapes() -> None:
    bad_kind: Any = "robot"
    with pytest.raises(ValidationError):
        participants.ParticipantPutParams(
            participant_id="p", workspace="w", kind=bad_kind, display_name="x"
        )
    with pytest.raises(ValidationError):
        participants.TurnAppendParams(
            session_id="s", speaker="p", text="", timestamp_ms=1
        )
    with pytest.raises(ValidationError):
        participants.ParticipantAskParams(subject="p", question="q", extra=1)  # type: ignore[call-arg]


@pytest.mark.spec("ARBORA-OPS-R897.2")
async def test_participant_put_and_member_add_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch)
    context = caller_context()
    await participants.handle_participant_put(
        context,
        {
            "participant_id": "ada",
            "workspace": "w",
            "kind": "person",
            "display_name": "Ada",
            "represents": None,
            "acts_as": ["a1"],
        },
        NO_OP,
    )
    method, params, idempotent = sent[0]
    assert (method, idempotent) == ("Participant", True)
    # The tenant is the verified caller's, never the body's.
    assert params["op"]["participant"] == {
        "participant_id": "ada",
        "tenant": "tenant:one",
        "workspace": "w",
        "kind": "person",
        "display_name": "Ada",
        "represents": None,
        "acts_as": ["a1"],
        "schema_version": 1,
    }
    await participants.handle_member_add(
        context, {"session_id": "s", "participant_id": "ada"}, NO_OP
    )
    assert sent[1][1] == {
        "op": {"op": "add_member", "session_id": "s", "participant_id": "ada"}
    }
    await participants.handle_participant_get(context, {"participant_id": "ada"}, NO_OP)
    await participants.handle_participant_list(context, {}, NO_OP)
    assert [(m, i) for m, _, i in sent[2:]] == [
        ("ParticipantRead", False),
        ("ParticipantRead", False),
    ]
    refused = _record(monkeypatch, EngineRefusal("MEMBERSHIP_SESSION_UNKNOWN"))
    with pytest.raises(EngineRefusal) as unknown:
        await participants.handle_member_add(
            context, {"session_id": "x", "participant_id": "ada"}, NO_OP
        )
    assert unknown.value.code == "MEMBERSHIP_SESSION_UNKNOWN" and refused


@pytest.mark.spec("ARBORA-OPS-R897.3")
async def test_scope_and_observation_round_trip_with_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch)
    context = caller_context()
    await participants.handle_scope_put(
        context, {"scope_id": "sc", "name": "n", "session_ids": ["s"]}, NO_OP
    )
    scope = sent[0][1]["op"]["scope"]
    assert scope["owner_principal"] == "person:one" and scope["tenant"] == "tenant:one"
    assert scope["shared_with"] == []
    config = {
        "session_id": "s",
        "participant_id": "ada",
        "observe_self": True,
        "observe_others": False,
    }
    await participants.handle_observation_set(context, config, NO_OP)
    assert sent[1][1] == {"op": {"op": "set_observation", "config": config}}
    _record(monkeypatch, EngineRefusal("OBSERVER_NOT_PERMITTED"))
    with pytest.raises(EngineRefusal) as refused:
        await participants.handle_observation_set(context, config, NO_OP)
    assert refused.value.code == "OBSERVER_NOT_PERMITTED"


@pytest.mark.spec("ARBORA-OPS-R897.4")
async def test_turn_append_commits_and_enqueues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch)
    context = caller_context()
    context.idempotency_key = "key-1"
    turn = {"session_id": "s", "speaker": "ada", "text": "hello", "timestamp_ms": 5}
    first = await participants.handle_turn_append(context, turn, NO_OP)
    again = await participants.handle_turn_append(context, turn, NO_OP)
    assert first == again and first["turn_committed"] is True
    assert first["derive"] == "enqueued"
    method, params, idempotent = sent[0]
    record = params["request"]["record"]
    assert (method, idempotent) == ("MemoryRemember", True)
    assert record["kind"] == "conversation_turn" and record["speaker"] == "ada"
    assert (
        record["tenant"] == "tenant:one" and record["memory_id"] == first["memory_id"]
    )


@pytest.mark.spec("ARBORA-OPS-R897.4")
async def test_backlog_refusal_keeps_the_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    _record(monkeypatch, EngineRefusal("DERIVE_BACKLOG_FULL"))
    context = caller_context()
    context.idempotency_key = "key-2"
    result = await participants.handle_turn_append(
        context,
        {"session_id": "s", "speaker": "ada", "text": "hi", "timestamp_ms": 1},
        NO_OP,
    )
    assert result["turn_committed"] is True and result["derive"] == "deferred"
    _record(monkeypatch, EngineRefusal("PARTICIPANT_NOT_VISIBLE"))
    with pytest.raises(EngineRefusal):
        await participants.handle_turn_append(
            context,
            {"session_id": "s", "speaker": "ada", "text": "hi", "timestamp_ms": 1},
            NO_OP,
        )


@pytest.mark.spec("ARBORA-OPS-R897.5")
async def test_ask_and_card_return_citations_or_typed_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answer = {"text": "t", "citations": [{"claim_id": "c", "turn_ids": ["t1"]}]}
    sent = _record(monkeypatch, answer)
    context = caller_context()
    ask = await participants.handle_participant_ask(
        context,
        {"subject": "ada", "question": "q", "observer": "bob", "memory_scope": "sc"},
        NO_OP,
    )
    assert ask == {"value": answer}
    question = sent[0][1]["op"]["question"]
    assert question["observer"] == {"participant": "bob"}
    assert question["memory_scope"] == {"scope_id": "sc"}
    await participants.handle_participant_card(context, {"subject": "ada"}, NO_OP)
    assert sent[1][1]["op"]["observer"] == "system"
    for code in ("NO_GROUNDED_CLAIMS", "CARD_ABSENT", "PARTICIPANT_NOT_VISIBLE"):
        _record(monkeypatch, EngineRefusal(code))
        with pytest.raises(EngineRefusal) as refused:
            await participants.handle_participant_card(
                context, {"subject": "ada"}, NO_OP
            )
        assert refused.value.code == code


def test_participant_operations_are_served_unavailable_until_the_scope_is_pinned(
    served: Any,  # noqa: F811
) -> None:
    app, _client = served
    for op_id, params in (
        ("participant.list", {}),
        ("participant.ask", {"subject": "ada", "question": "q"}),
    ):
        reply = _call(app, op_id, params)
        assert reply.status_code == 503, reply.text
        assert reply.json()["error"]["code"] == "UNAVAILABLE"
