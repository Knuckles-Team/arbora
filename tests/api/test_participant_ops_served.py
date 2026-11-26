"""Participant memory operations (ARBORA-OPS-R897.1 to R897.5, R897.8)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.ops import participants
from tests.api._knowledge_support import NO_OP, caller_context
from tests.test_served_operation_api import _call, _value, served  # noqa: F401


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


class _Loopback:
    """A loopback engine that keeps participants and answers per tenant."""

    def __init__(self) -> None:
        self.participants: dict[tuple[str, str], dict[str, Any]] = {}
        self.members: list[tuple[str, str]] = []
        self.turns: list[dict[str, Any]] = []
        self.profiles: dict[str, dict[str, Any]] = {}
        self.tenant = "tenant:one"

    def _tenant(self, principal: str) -> str:
        return "tenant:two" if principal == "person:two" else "tenant:one"

    def participant(self, params: dict[str, Any], principal: str) -> Any:
        from medulla import EngineResponseError

        op, tenant = params["op"], self._tenant(principal)
        if op["op"] == "put":
            row = op["participant"]
            self.participants[(tenant, row["participant_id"])] = row
            return {"stored": row["participant_id"]}
        if op["op"] == "add_member":
            self.members.append((op["session_id"], op["participant_id"]))
            return {"members": len(self.members)}
        if op["op"] in ("ask", "card"):
            subject = op.get("subject") or op["question"]["subject"]
            if (tenant, subject) not in self.participants:
                # A participant of another tenant answers like an absent one.
                raise EngineResponseError(
                    "NO_GROUNDED_CLAIMS" if op["op"] == "ask" else "CARD_ABSENT", None
                )
            return {"citations": [{"claim": "likes tea"}], "subject": subject}
        raise AssertionError(op)

    def read(self, params: dict[str, Any], principal: str) -> Any:
        op = params["op"]
        if op["op"] == "list":
            tenant = self._tenant(principal)
            return {
                "participants": sorted(p for t, p in self.participants if t == tenant)
            }
        return self.participant(params, principal)

    def remember(self, params: dict[str, Any], principal: str) -> Any:
        self.turns.append(params["request"]["record"])
        return {"stored": True}

    def profile(self, params: dict[str, Any], principal: str) -> Any:
        op = params["op"]
        if op["op"] == "put_profile":
            self.profiles[op["profile"]["profile_id"]] = op["profile"]
            return {"revision": 1}
        raise AssertionError(op)


@pytest.fixture
def unpinned(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pinned engine catalog holds the participant and profile scopes."""
    from arbora.api.ops import registry_factory
    from arbora.security import request_identity

    monkeypatch.setattr(
        request_identity,
        "_GRAPH_AUTH_SCOPES",
        request_identity._GRAPH_AUTH_SCOPES
        | {"participant:read", "participant:write", "profile:write"},
    )
    monkeypatch.setattr(
        registry_factory,
        "apply_pending_scopes",
        lambda curated, _scopes, pending=None: (list(curated), []),
    )


class _Runs:
    """The A2A service double: it records the message a run was sent."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_message(self, **params: Any) -> Any:
        from types import SimpleNamespace

        self.messages.append(params["message"])
        return SimpleNamespace(model_dump=lambda **_kw: {"id": "task-1"})


@pytest.fixture
def captured(
    unpinned: None, monkeypatch: pytest.MonkeyPatch
) -> tuple[dict[str, Any], _Runs]:
    """The served invocation, with the run service replaced by a recorder."""
    import tests.test_served_operation_api as harness

    runs = _Runs()
    held: dict[str, Any] = {}
    compose = harness.compose_served_invocation

    def composing(**kwargs: Any) -> Any:
        kwargs["bindings"] = {**kwargs["bindings"], "a2a": runs}
        held["invocation"] = compose(**kwargs)
        return held["invocation"]

    monkeypatch.setattr(harness, "compose_served_invocation", composing)
    return held, runs


@pytest.mark.spec("ARBORA-OPS-R897.8")
def test_participant_memory_journey(
    captured: tuple[dict[str, Any], _Runs],
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.api._served_support import McpAsk, route_methods

    held, runs = captured
    from tests.api._served_support import person as _person

    app, client = served
    engine = _Loopback()
    grants = ("participant:read", "participant:write", "profile:write")
    one = _person("one", extra_scopes=grants)
    # The engine scopes by the verified principal; person two is another tenant there.
    two = _person("two", extra_scopes=grants)
    route_methods(
        client,
        {
            "Participant": engine.participant,
            "ParticipantRead": engine.read,
            "MemoryRemember": engine.remember,
            "AssistantProfile": engine.profile,
        },
        monkeypatch,
        {"fixture-verified": one, "two": two},
    )
    key = {"Idempotency-Key": "journey-1"}
    for name in ("ada", "grace"):
        put = _call(
            app,
            "participant.put",
            {
                "participant_id": name,
                "workspace": "w",
                "kind": "person",
                "display_name": name,
            },
            **{"Idempotency-Key": f"put-{name}"},
        )
        assert put.status_code == 200, put.text
        added = _call(
            app,
            "participant.member.add",
            {"session_id": "s1", "participant_id": name},
            **{"Idempotency-Key": f"add-{name}"},
        )
        assert added.status_code == 200, added.text
    for index, speaker in enumerate(("ada", "grace")):
        turn = _call(
            app,
            "participant.turn.append",
            {
                "session_id": "s1",
                "speaker": speaker,
                "text": "I like tea",
                "timestamp_ms": 1000 + index,
            },
            **{"Idempotency-Key": f"turn-{index}"},
        )
        assert _value(turn)["turn_committed"] is True
    assert _value(_call(app, "participant.list", {}))["participants"] == [
        "ada",
        "grace",
    ]
    asked = _value(
        _call(app, "participant.ask", {"subject": "ada", "question": "tea?"})
    )
    assert asked["citations"] == [{"claim": "likes tea"}]
    assert (
        _value(_call(app, "participant.card", {"subject": "ada"}))["subject"] == "ada"
    )
    saved = _call(
        app,
        "assistant.profile.put",
        {"profile_id": "p1", "name": "n", "instructions": "be brief"},
        **key,
    )
    assert saved.status_code == 200, saved.text
    assert engine.profiles["p1"]["tenant"] == "tenant:one"
    # The run carries the saved profile, over HTTP and then over MCP.
    ran = _call(
        app,
        "agents.run",
        {"message": {"parts": []}, "assistant_profile_ref": "p1"},
        **{"Idempotency-Key": "run-1"},
    )
    assert ran.status_code == 200, ran.text
    ask = McpAsk(held["invocation"], monkeypatch, one)
    answered = ask("ask", "participant.ask", {"subject": "grace", "question": "tea?"})
    assert answered["result"]["value"]["citations"] == [{"claim": "likes tea"}], (
        answered
    )
    card = ask("ask", "participant.card", {"subject": "ada"})
    assert card["result"]["value"]["subject"] == "ada", card
    assert [m["metadata"]["assistant_profile_ref"] for m in runs.messages] == ["p1"]
    assert len(engine.turns) == 2 and engine.members == [
        ("s1", "ada"),
        ("s1", "grace"),
    ]

    # The same ask as the second tenant answers exactly like an absent subject.
    foreign = _call(
        app,
        "participant.ask",
        {"subject": "ada", "question": "tea?"},
        Authorization="Bearer two",
    )
    absent = _call(app, "participant.ask", {"subject": "nobody", "question": "tea?"})
    assert foreign.status_code == absent.status_code, foreign.text
    assert foreign.json()["error"]["code"] == absent.json()["error"]["code"]
    assert "ada" not in json.dumps(foreign.json()["error"].get("details"))
