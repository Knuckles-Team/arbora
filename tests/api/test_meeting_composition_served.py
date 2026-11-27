"""``meetings.summary.create`` is reachable with the production bindings (ARBORA-OPS-R065.3)."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import meeting_engine, meetings
from arbora.api.registry import Verb
from arbora.mcp_server.served_invocation import _with_meeting_summarizer
from tests.api._engine_double import EngineDouble
from tests.api._engine_double import context as engine_context

REQUEST = {
    "schema_version": 1,
    "meeting_id": "m-1",
    "transcript_revision": 2,
    "provider_id": "prov",
    "context_revision": 5,
}
EVENTS = [
    {"sequence": 1, "revision": 1, "op": "append", "text_digest": "d" * 64},
    {
        "sequence": 2,
        "revision": 2,
        "op": "finalize",
        "text_digest": "e" * 64,
        "finalization_digest": "seal-1",
        "segment_ids": ["s1"],
    },
]
SEGMENTS = [{"segment_id": "s1", "text": "hello"}]
HELLO = hashlib.sha256(b"hello").hexdigest()
SEALED = {"finalization_digest": "seal-1", "segment_digests": {"s1": HELLO}}
UNSEALED = {"finalization_digest": None, "segment_digests": {}}
SENDERS = ("ingestion", "send_meeting_transcript_read")


class Gateway:
    def __init__(self) -> None:
        item = SimpleNamespace(
            id="prov",
            name="summarize",
            server="notes",
            annotations={
                "capability": "meeting_summary",
                "execution_class": "hosted",
                "model_id": "mod",
            },
        )
        text = SimpleNamespace(
            id="source", name="get_transcript", server="audio", annotations={}
        )

        async def with_capability(caller: Any, capability: str) -> tuple[Any, ...]:
            return {"meeting_transcript": (text,)}.get(capability, (item,))

        self.catalog = SimpleNamespace(with_capability=with_capability)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def effect(self, op: Any, params: Any, caller: Any) -> Any:
        return SimpleNamespace(effect="read")

    async def call(self, caller: Any, server: str, tool: str, arguments: Any, **_: Any):
        self.calls.append((server, tool, arguments))
        return {
            "get_transcript": {"segments": SEGMENTS},
        }.get(tool, {"claims": [], "receipt": {"receipt_id": "r1"}})


def _wired(
    monkeypatch: pytest.MonkeyPatch, *, allow: frozenset[str], sealed: bool = True
) -> tuple[Any, EngineDouble, Gateway]:
    engine = EngineDouble(monkeypatch)

    def read(params: dict[str, Any]) -> dict[str, Any]:
        asked = params["request"]
        page = [e for e in EVENTS if e["sequence"] > asked["after_seq"]]
        return {
            "meeting_id": asked["meeting_id"],
            "events": page[: asked["limit"]],
            "last_sequence": 2,
            "transcript_revision": 2,
            "context_revision": 5,
            **(SEALED if sealed else UNSEALED),
        }

    def grant(params: dict[str, Any]) -> dict[str, Any]:
        return {
            "allow": params["operation"] in allow,
            "revocation_revision": 4,
            "scope_ceiling": [],
        }

    engine.serve("MeetingTranscriptRead", read, sender=SENDERS)
    engine.serve("CheckResourceAccess", grant)
    engine.serve("MeetingSummaryContext", lambda p: {"context_digest": "c" * 64})
    engine.serve("MeetingSummaryCommit", lambda p: {"claim_count": 0})
    for module in (meeting_engine, meetings):
        engine.install(module)
    gateway = Gateway()
    context = engine_context()
    context.service_identity = False
    context.owner = "principal:one"
    context.registry_digest = ""
    bound = _with_meeting_summarizer({"fleet_gateway": gateway})
    assert bound is not None
    context.services = dict(bound)
    return context, engine, gateway


def _op() -> Any:
    return next(o for o in meetings.operations() if o.id == "meetings.summary.create")


def _assert_text_then_provider(engine: Any, gateway: Any) -> None:
    """The engine holds digests, so the text came from the fleet tool, then the provider ran."""
    (text_call, run_call) = gateway.calls
    assert text_call[:2] == ("audio", "get_transcript")
    assert text_call[2]["finalization_digest"] == "seal-1"
    assert run_call[:2] == ("notes", "summarize")
    assert run_call[2]["model"] == "mod"
    asked = next(p for m, p, _ in engine.sent if m == "MeetingTranscriptRead")
    assert set(asked) == {"request"} and asked["request"]["limit"] <= 500


@pytest.mark.spec("ARBORA-OPS-R065.3")
async def test_summary_create_is_reachable_with_production_bindings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, engine, gateway = _wired(
        monkeypatch, allow=frozenset({"capture", "egress"})
    )
    assert _op().verb is Verb.ACT
    result = await meetings.handle_summary_create(context, REQUEST, _op())
    assert result["value"]["summary_id"].startswith("sum-")
    assert result["value"]["provenance"]["provider"] == "prov"
    sent = [method for method, _, _ in engine.sent]
    assert sent.count("CheckResourceAccess") == 2
    assert sent[-2:] == ["MeetingSummaryContext", "MeetingSummaryCommit"]
    context_request = next(p for m, p, _ in engine.sent if m == "MeetingSummaryContext")
    (segment,) = context_request["request"]["segments"]
    assert segment["text"] == "hello"
    _assert_text_then_provider(engine, gateway)


@pytest.mark.spec("ARBORA-OPS-R065.4")
async def test_production_admission_refuses_egress_before_any_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, engine, gateway = _wired(monkeypatch, allow=frozenset({"capture"}))
    with pytest.raises(OperationRefused) as denied:
        await meetings.handle_summary_create(context, REQUEST, _op())
    assert denied.value.code == "POLICY_DENIED"
    assert denied.value.details == {"reason": "EGRESS_DENIED"}
    assert gateway.calls == []
    assert "MeetingSummaryContext" not in [m for m, _, _ in engine.sent]


@pytest.mark.spec("ARBORA-OPS-R065.4")
async def test_production_ports_refuse_stale_unsealed_and_foreign_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, _, gateway = _wired(monkeypatch, allow=frozenset({"capture", "egress"}))
    with pytest.raises(OperationRefused) as stale:
        await meetings.handle_summary_create(
            context, {**REQUEST, "transcript_revision": 3}, _op()
        )
    assert stale.value.code == "STALE_REVISION"
    with pytest.raises(OperationRefused) as stale_context:
        await meetings.handle_summary_create(
            context, {**REQUEST, "context_revision": 4}, _op()
        )
    assert stale_context.value.code == "STALE_REVISION"
    unsealed, _, _ = _wired(
        monkeypatch, allow=frozenset({"capture", "egress"}), sealed=False
    )
    with pytest.raises(OperationRefused) as early:
        await meetings.handle_summary_create(unsealed, REQUEST, _op())
    assert early.value.code == "POLICY_DENIED"
    with pytest.raises(OperationRefused) as foreign:
        await context.services["meeting_final"].get_final(
            request=context, tenant="tenant:two", meeting_id="m-1"
        )
    assert foreign.value.code == "TENANT_DENIED"
    assert gateway.calls == []


@pytest.mark.spec("ARBORA-OPS-R065.4")
async def test_a_segment_text_that_misses_the_engine_digest_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context, engine, gateway = _wired(
        monkeypatch, allow=frozenset({"capture", "egress"})
    )
    monkeypatch.setitem(SEGMENTS[0], "text", "tampered")
    with pytest.raises(OperationRefused) as refused:
        await meetings.handle_summary_create(context, REQUEST, _op())
    assert refused.value.code == "UNAVAILABLE"
    assert "MeetingSummaryContext" not in [m for m, _, _ in engine.sent]
    assert [call[1] for call in gateway.calls] == ["get_transcript"]
