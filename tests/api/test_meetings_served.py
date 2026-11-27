"""Meeting transcript, provider and summary models (ARBORA-OPS-R065.1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from arbora.api.ops.meetings import (
    ProviderChoice,
    SummaryRequest,
    SummaryResult,
    TranscriptEvent,
)

EVENT = {
    "schema_version": 1,
    "meeting_id": "m1",
    "segment_id": "s1",
    "sequence": 4,
    "correction_revision": 1,
    "state": "final",
    "start_ms": 1000,
    "end_ms": 2500,
    "text": "agreed",
    "source_id": "mic-1",
    "model_id": "speech-small",
}


@pytest.mark.spec("ARBORA-OPS-R065.1")
def test_models_round_trip_vectors() -> None:
    models = {
        TranscriptEvent: EVENT,
        ProviderChoice: {
            "provider_id": "p1",
            "execution_class": "hosted",
            "model_id": "m",
        },
        SummaryRequest: {
            "schema_version": 1,
            "meeting_id": "m1",
            "transcript_revision": 9,
            "provider_id": "p1",
            "context_revision": "ctx-3",
        },
        SummaryResult: {
            "schema_version": 1,
            "summary_id": "sum-1",
            "text": "decided",
            "provenance": {"provider": "p1", "model": "m"},
            "state": "complete",
        },
    }
    for model, vector in models.items():
        assert model.model_validate(vector).model_dump() == vector
    for state in ("partial", "final", "finalized"):
        assert TranscriptEvent.model_validate({**EVENT, "state": state}).state == state


@pytest.mark.spec("ARBORA-OPS-R065.1")
def test_closed_values_and_extra_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        TranscriptEvent.model_validate({**EVENT, "state": "draft"})
    with pytest.raises(ValidationError):
        TranscriptEvent.model_validate({**EVENT, "extra": 1})
    with pytest.raises(ValidationError):
        ProviderChoice.model_validate(
            {"provider_id": "p", "execution_class": "cloud", "model_id": "m"}
        )
    with pytest.raises(ValidationError):
        TranscriptEvent.model_validate({**EVENT, "sequence": -1})


# ---- transcript stream, providers and summary (ARBORA-OPS-R065.2 to R065.5) ----

import types  # noqa: E402
from typing import Any  # noqa: E402

from starlette.websockets import WebSocketDisconnect  # noqa: E402

from arbora.api.invoke.pipeline import OperationRefused  # noqa: E402
from arbora.api.ops import meetings  # noqa: E402
from arbora.api.registry import Confirm  # noqa: E402
from tests.api._engine_double import EngineDouble, context  # noqa: E402
from tests.api._meeting_fakes import (  # noqa: E402
    REQUEST,
    Admission,
    Final,
    Summarizer,
    gateway,
    install_engine,
    meeting_context,
    provider,
)

OPS = {op.id: op for op in meetings.operations()}
ALLOWED = {"allowed": True, "policy_revision": "p1", "admission_id": "a1"}


def segment(sequence: int, **changes: Any) -> dict[str, Any]:
    return {
        "segment_id": f"s{sequence}",
        "sequence": sequence,
        "correction_revision": 0,
        "state": "final",
        "start_ms": sequence * 1000,
        "end_ms": sequence * 1000 + 900,
        "text": f"words {sequence}",
        "source_id": "mic-1",
        "model_id": "speech-small",
        **changes,
    }


def transcript_engine(monkeypatch, events, *, last=None, digest=None):
    """The w18 ``MeetingTranscriptRead`` as a structural engine double."""
    engine = EngineDouble(monkeypatch)
    state = {"events": events, "last": last, "digest": digest}

    def read(params):
        after = params["after_seq"]
        rows = [e for e in state["events"] if e["sequence"] > after]
        newest = max((e["sequence"] for e in state["events"]), default=0)
        return {
            "events": rows[: params["limit"]],
            "last_sequence": state["last"] if state["last"] is not None else newest,
            "finalization_digest": state["digest"],
        }

    engine.serve("MeetingTranscriptRead", read)
    engine.install(meetings)
    return engine, state


async def subscribe(cursor=0, **changes):
    params = {"meeting_id": "m1", "cursor": cursor, **changes}
    ctx = context(scopes=frozenset({"meetings:read"}))
    return (
        await meetings.handle_transcript_subscribe(
            ctx, params, OPS["meetings.transcript.subscribe"]
        )
    )["value"]


@pytest.mark.spec("ARBORA-OPS-R065.2")
async def test_corrections_gaps_and_finalization_are_ordered(monkeypatch) -> None:
    events = [
        segment(2, state="partial"),
        segment(1),
        segment(3, correction_revision=1, segment_id="s2"),
        segment(5),
    ]
    _, state = transcript_engine(monkeypatch, events)
    page = await subscribe()
    # Order by sequence; the hole at 4 is a gap, distinct from unavailable.
    assert [e["sequence"] for e in page["events"]] == [1, 2, 3]
    assert page["events"][2]["correction_revision"] == 1
    assert page["gap"] == {"after": 3, "missing": 4}
    assert page["finalized"] is False and page["cursor"] == 3
    assert page["stream_path"] == "/ws/meetings/m1/transcript"
    # `finalized` never appears while a sequence is missing or no seal exists.
    state["events"] = [segment(1, state="finalized"), segment(3)]
    assert [e["state"] for e in (await subscribe())["events"]] == []
    state["events"] = [segment(n, state="finalized") for n in (1, 2, 3, 4, 5)]
    state["digest"] = "seal"
    done = await subscribe(cursor=3)
    assert [e["sequence"] for e in done["events"]] == [4, 5]
    assert done["gap"] is None and done["finalized"] is True


@pytest.mark.spec("ARBORA-OPS-R065.2")
async def test_stale_cursor_and_scope_and_pinned_engine_refuse(monkeypatch) -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    transcript_engine(monkeypatch, [segment(1)])
    with pytest.raises(OperationRefused) as stale:
        await subscribe(cursor=9)
    assert stale.value.code == "STALE_CURSOR"
    assert OPS["meetings.transcript.subscribe"].scopes == {"meetings:read"}
    # The pinned release lacks the w18 method: UNAVAILABLE naming it, no stand-in.
    import medulla.generated as generated

    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) - {"MeetingTranscriptRead"},
    )
    with pytest.raises(OperationRefused) as pinned:
        await subscribe()
    assert pinned.value.code == "UNAVAILABLE"
    assert pinned.value.details["missing"] == "MeetingTranscriptRead"


@pytest.mark.spec("ARBORA-OPS-R065.2")
async def test_invalid_engine_transcript_is_unavailable_not_repaired(
    monkeypatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    engine.serve(
        "MeetingTranscriptRead", lambda p: {"events": [{"text": 1}], "last_sequence": 0}
    )
    engine.install(meetings)
    with pytest.raises(OperationRefused) as bad:
        await subscribe()
    assert bad.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R065.3")
async def test_each_local_and_hosted_route_records_provenance(monkeypatch) -> None:
    gw = gateway(
        ("loc", provider("local", "small")),
        ("hos", provider("hosted", "large")),
        ("none", {"capability": "other", "execution_class": "local", "model_id": "x"}),
        ("bare", {"capability": "meeting_summary", "model_id": "x"}),
    )
    ctx = meeting_context(Admission(ALLOWED), fleet_gateway=gw)
    listed = (
        await meetings.handle_providers_list(ctx, {}, OPS["meetings.providers.list"])
    )["value"]["providers"]
    # A provider without an explicit execution class is not offered at all.
    assert listed == [
        {"provider_id": "hos", "execution_class": "hosted", "model_id": "large"},
        {"provider_id": "loc", "execution_class": "local", "model_id": "small"},
    ]
    calls = install_engine(monkeypatch, meetings)
    for choice in listed:
        ctx = meeting_context(Admission(ALLOWED), fleet_gateway=gw)
        request = {**REQUEST, "provider_id": choice["provider_id"]}
        made = await meetings.handle_summary_create(
            ctx, request, OPS["meetings.summary.create"]
        )
        assert made["value"]["provenance"] == {
            "provider": choice["provider_id"],
            "model": choice["model_id"],
            "context_revision": "ctx-3",
            "transcript_revision": "9",
        }
    assert calls == ["MeetingSummaryContext", "MeetingSummaryCommit"] * 2
    assert OPS["meetings.summary.create"].confirm is Confirm.PLAN


@pytest.mark.spec("ARBORA-OPS-R065.3")
async def test_summary_get_reads_the_stored_result_and_refuses_when_pinned(
    monkeypatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    stored = {
        "schema_version": 1,
        "summary_id": "sum-1",
        "text": "decided",
        "provenance": {"provider": "hos", "model": "large"},
        "state": "complete",
    }
    engine.serve("MeetingSummaryGet", lambda params: stored)
    engine.install(meetings)
    ctx = context(scopes=frozenset({"meetings:read"}))
    op = OPS["meetings.summary.get"]
    got = await meetings.handle_summary_get(ctx, {"meeting_id": "m1"}, op)
    assert got["value"] == stored
    assert engine.sent == [("MeetingSummaryGet", {"meeting_id": "m1"}, False)]
    engine.handlers["MeetingSummaryGet"] = lambda params: {"summary_id": ""}
    with pytest.raises(OperationRefused) as bad:
        await meetings.handle_summary_get(ctx, {"meeting_id": "m1"}, op)
    assert bad.value.code == "UNAVAILABLE"
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"MeetingSummaryGet"}
    )
    with pytest.raises(OperationRefused) as pinned:
        await meetings.handle_summary_get(ctx, {"meeting_id": "m1"}, op)
    assert pinned.value.details["missing"] == "MeetingSummaryGet"


@pytest.mark.spec("ARBORA-OPS-R065.4")
async def test_denied_egress_and_stale_revision_make_zero_provider_calls(
    monkeypatch,
) -> None:
    calls = install_engine(monkeypatch, meetings)
    summarizer = Summarizer()
    op = OPS["meetings.summary.create"]
    for code in ("EGRESS_DENIED", "CAPTURE_DENIED"):
        ctx = meeting_context(
            Admission({"allowed": False, "code": code}), summarizer=summarizer
        )
        with pytest.raises(OperationRefused) as denied:
            await meetings.handle_summary_create(ctx, REQUEST, op)
        assert denied.value.code == "POLICY_DENIED"
        assert denied.value.details == {"reason": code}
    for stale in (
        {"transcript_revision": 10},
        {"context_revision": "ctx-9"},
    ):
        ctx = meeting_context(Admission(ALLOWED), summarizer=summarizer)
        with pytest.raises(OperationRefused) as refused:
            await meetings.handle_summary_create(ctx, {**REQUEST, **stale}, op)
        assert refused.value.code == "STALE_REVISION"
    unsealed = meeting_context(
        Admission(ALLOWED), summarizer=summarizer, meeting_final=Final(digest="")
    )
    with pytest.raises(OperationRefused) as early:
        await meetings.handle_summary_create(unsealed, REQUEST, op)
    assert early.value.code == "POLICY_DENIED"
    missing = meeting_context(Admission(ALLOWED), summarizer=summarizer)
    with pytest.raises(OperationRefused) as nobody:
        await meetings.handle_summary_create(
            missing, {**REQUEST, "provider_id": "ghost"}, op
        )
    assert nobody.value.code == "UNAVAILABLE"
    assert summarizer.calls == 0 and calls == []


def stream_app(monkeypatch, ops, steps):
    """The gateway app with the transcript route; each poll runs the real handler.

    ``steps`` is a list of callables run between polls (they move the engine double
    forward); after the last one the controlled clock ends the stream.
    """
    from fastapi import FastAPI

    from arbora.gateway import stream_poll
    from arbora.gateway.meetings_ws import register_meeting_routes

    ctx = context(scopes=frozenset({"meetings:read"}))
    polls = []

    async def invoke_operation(op_id, params, *, request, idempotency_key=None):
        polls.append((op_id, params))
        return await meetings.handle_transcript_subscribe(ctx, params, ops[op_id])

    async def pause(websocket):
        if not steps:
            raise stream_poll.StopStream
        steps.pop(0)()

    monkeypatch.setattr(
        stream_poll,
        "gateway_application",
        lambda: types.SimpleNamespace(invoke_operation=invoke_operation),
    )
    monkeypatch.setattr(stream_poll, "wait_for_next_poll", pause)
    app = FastAPI()
    register_meeting_routes(app)
    return app, polls


@pytest.mark.spec("ARBORA-OPS-R065.5")
def test_ordered_corrections_finalization_and_provider_routes(monkeypatch) -> None:
    from starlette.testclient import TestClient

    _, state = transcript_engine(monkeypatch, [segment(1, state="partial")])

    def correct():
        state["events"] += [segment(2, correction_revision=1, segment_id="s1")]

    def finish():
        state["events"] += [segment(3, state="finalized")]
        state["digest"] = "seal"

    app, polls = stream_app(monkeypatch, OPS, [correct, finish])
    with TestClient(app).websocket_connect("/ws/meetings/m1/transcript") as socket:
        frames = [socket.receive_json() for _ in range(3)]
    assert [f["events"][0]["sequence"] for f in frames] == [1, 2, 3]
    assert [f["finalized"] for f in frames] == [False, False, True]
    assert frames[1]["events"][0]["correction_revision"] == 1
    # The cursor moves forward only; each poll is the served operation again.
    assert [p[1]["cursor"] for p in polls] == [0, 1, 2]
    assert {p[0] for p in polls} == {"meetings.transcript.subscribe"}


@pytest.mark.spec("ARBORA-OPS-R065.5")
def test_stream_reports_a_gap_and_closes_on_refusals(monkeypatch) -> None:
    from starlette.testclient import TestClient

    from arbora.api.http.auth import HTTPAuthenticationError

    transcript_engine(monkeypatch, [segment(1), segment(3)])
    app, _ = stream_app(monkeypatch, OPS, [])
    with TestClient(app).websocket_connect("/ws/meetings/m1/transcript") as socket:
        frame = socket.receive_json()
        assert frame["gap"] == {"after": 1, "missing": 2}
    # A stale cursor closes 4409; a revoked session closes 4401 with no frame.
    app, _ = stream_app(monkeypatch, OPS, [])
    with TestClient(app).websocket_connect(
        "/ws/meetings/m1/transcript?cursor=9"
    ) as socket:
        assert socket.receive_json()["code"] == "STALE_CURSOR"
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4409
    from arbora.gateway import stream_poll

    async def revoked(op_id, params, *, request, idempotency_key=None):
        raise HTTPAuthenticationError("revoked")

    monkeypatch.setattr(
        stream_poll,
        "gateway_application",
        lambda: types.SimpleNamespace(invoke_operation=revoked),
    )
    with TestClient(app).websocket_connect("/ws/meetings/m1/transcript") as socket:
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_json()
        assert closed.value.code == 4401
