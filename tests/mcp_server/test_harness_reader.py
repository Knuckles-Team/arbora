"""Native harness reader admission and projection (ARBORA-OPS-R068).

Source-level wiring tests: the installed engine distribution lacks the
harness wire methods, so every read answers unavailable without touching
the engine. A test-supplied wire module proves the admission, claim
binding and projection only; installed native acceptance remains pending
the published engine contract carrying the harness queries.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

from arbora.api.invoke.executor import ExecutionContext
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import VerifiedCaller
from arbora.api.ops.harness_components import (
    UNAVAILABLE_COMPONENT_GET,
    UNAVAILABLE_COMPONENT_LIST,
)
from arbora.api.ops.harness_evolution import UNAVAILABLE_EVOLUTION_REPORT
from arbora.api.ops.harness_lessons import UNAVAILABLE_LESSON_RETRIEVAL
from arbora.mcp_server.harness_reader import (
    NativeComponentReader,
    NativeEvolutionReportReader,
    NativeLessonRetriever,
)
from tests._support import Actor

WIRE_MODULE = "epistemic_graph.generated.orchestration"

DIGEST = "ab" * 32
CONTRAST_DIGEST = "cf" * 32
CONDITIONS_DIGEST = "cd" * 32


class _WireModule:
    """A test-supplied stand-in for the generated engine wire module."""

    def __init__(self, results: dict[str, object] | None = None) -> None:
        self.sent: list[tuple[str, dict[str, object], str]] = []
        self.results = results or {}

    def _sender(self, name: str, result: object, raise_error: object | None):
        async def send(client, payload, graph):
            self.sent.append((name, payload, graph))
            if raise_error is not None:
                raise raise_error
            return result

        return send

    def _decoder(self, name: str, answer: object):
        def decode(result):
            return answer

        return decode

    def module(self) -> SimpleNamespace:
        return SimpleNamespace(
            send_list_harness_components=self._sender(
                "list", self.results.get("list"), None
            ),
            decode_list_harness_components=self._decoder(
                "list", self.results.get("list")
            ),
            send_current_harness_component=self._sender(
                "current", self.results.get("current"), None
            ),
            decode_current_harness_component=self._decoder(
                "current", self.results.get("current")
            ),
            send_retrieve_lessons=self._sender(
                "lessons", self.results.get("lessons"), None
            ),
            decode_retrieve_lessons=self._decoder(
                "lessons", self.results.get("lessons")
            ),
            send_harness_evolution_report=self._sender(
                "report", self.results.get("report"), None
            ),
            decode_harness_evolution_report=self._decoder(
                "report", self.results.get("report")
            ),
        )


class _Client:
    def __init__(self) -> None:
        self.claims = ContextVar("harness-reader-test-claims", default=None)
        self.uses: list[dict[str, object]] = []

    @contextmanager
    def use_verified_context(self, claims):
        self.uses.append(claims)
        token = self.claims.set(claims)
        try:
            yield self
        finally:
            self.claims.reset(token)


def _context(
    *,
    client: _Client,
    scopes=("kg:read",),
    service_identity: bool = False,
    principal: str = "caller-a",
    tenant: str = "tenant-a",
) -> ExecutionContext:
    claims = {
        "principal": principal,
        "tenant": tenant,
        "scopes": list(scopes),
        "policy_version": "v1",
        "delegation": [],
    }
    actor = Actor(actor_id=principal, tenant_id=tenant)
    session = SimpleNamespace(
        actor=actor,
        tenant=tenant,
        graph="control",
        engine_verified_context=lambda: claims,
    )
    caller = VerifiedCaller(
        principal=principal,
        tenant=tenant,
        effective_scopes=frozenset(scopes),
        engine_claims=claims,
        principal_kind="human" if not service_identity else "service",
        authenticated=True,
        delegated=False,
        credential_kind="session",
        policy_revision="v1",
        session=session,
    )
    return ExecutionContext(
        caller=caller,
        client=client,
        owner=principal,
        service_identity=service_identity,
        services={},
    )


def _component_version(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "component_id": "comp-one",
        "kind": "guardrails",
        "content_ref": "blob:one",
        "content_digest": DIGEST,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": None,
        "confidence": 0.9,
        "evidence_run_id": "run-one",
        "supersedes_id": None,
        "applied_to_id": None,
    }
    values.update(changes)
    return values


def _lesson(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "task_class": "task:retrieval",
        "deficiency": "stale-context",
        "situation": "long task",
        "changed_component_kind": "memory",
        "what_changed": "prune context",
        "applicability": "long tasks",
        "confidence": 0.7,
        "evidence_run_ids": ["run-a"],
        "supersedes_id": None,
    }
    values.update(changes)
    return values


def _report(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "decision_id": "decision-one",
        "task_ref": "task-one",
        "task_class": "task:retrieval",
        "conditions_digest": CONDITIONS_DIGEST,
        "contrast_digest": CONTRAST_DIGEST,
        "probe_trace_ids": ["trace-a"],
        "deltas": [
            {
                "candidate_version_ref": "ver-a",
                "retry_delta": -1.0,
                "tool_selection_delta": 0.0,
                "eval_score_delta": 0.2,
                "cost_delta": -0.1,
            }
        ],
        "adopted_version_ref": "ver-a",
        "abstain_reason": None,
    }
    values.update(changes)
    return values


def _run(coro):
    return asyncio.run(coro)


def test_installed_distribution_reads_unavailable_without_engine_calls() -> None:
    """The pinned distribution lacks the harness wire module: every read
    answers unavailable and the engine is never contacted."""
    client = _Client()
    context = _context(client=client)
    assert WIRE_MODULE not in sys.modules
    assert (
        _run(
            NativeComponentReader().list(
                context=context, cursor=None, limit=10, kind=None
            )
        )
        == UNAVAILABLE_COMPONENT_LIST
    )
    assert (
        _run(NativeComponentReader().get(context=context, component_id="c", as_of=None))
        == UNAVAILABLE_COMPONENT_GET
    )
    assert (
        _run(
            NativeLessonRetriever().retrieve(
                context=context,
                task_class="t",
                deficiency="d",
                trajectory_features={},
            )
        )
        == UNAVAILABLE_LESSON_RETRIEVAL
    )
    assert (
        _run(NativeEvolutionReportReader().report(context=context, task_ref="task-one"))
        == UNAVAILABLE_EVOLUTION_REPORT
    )
    assert WIRE_MODULE not in sys.modules
    # Each read entered the caller-bound path and bound the verified context,
    # but the bare client has no wire sender: no engine call could run.
    assert len(client.uses) == 4


def test_missing_kg_read_refuses_before_any_wire_call(monkeypatch) -> None:
    wire = _WireModule()
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client, scopes=())
    with pytest.raises(OperationRefused) as excinfo:
        _run(
            NativeLessonRetriever().retrieve(
                context=context,
                task_class="t",
                deficiency="d",
                trajectory_features={},
            )
        )
    assert excinfo.value.code == "SCOPE_REQUIRED"
    assert wire.sent == []
    assert client.uses == []


def test_service_identity_refuses_before_any_wire_call(monkeypatch) -> None:
    wire = _WireModule()
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client, service_identity=True)
    with pytest.raises(OperationRefused) as excinfo:
        _run(NativeEvolutionReportReader().report(context=context, task_ref="task-one"))
    assert excinfo.value.code == "PRINCIPAL_NOT_ALLOWED"
    assert wire.sent == []


def test_component_get_binds_verified_claims_and_projects(monkeypatch) -> None:
    wire = _WireModule(results={"current": _component_version()})
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client)
    read = _run(
        NativeComponentReader().get(
            context=context, component_id="comp-one", as_of=None
        )
    )
    assert wire.sent == [
        ("current", {"component_id": "comp-one", "as_of": None}, "control")
    ]
    assert len(client.uses) == 1
    assert client.uses[0]["principal"] == "caller-a"
    assert client.uses[0]["tenant"] == "tenant-a"
    assert read.availability == "available"
    assert read.entry is not None
    assert read.entry.content_digest == DIGEST
    assert read.entry.kind == "guardrails"


def test_component_list_projects_every_entry(monkeypatch) -> None:
    wire = _WireModule(
        results={"list": [_component_version(), _component_version(component_id="c2")]}
    )
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client)
    read = _run(
        NativeComponentReader().list(
            context=context, cursor=None, limit=5, kind="skill"
        )
    )
    assert wire.sent == [
        ("list", {"cursor": None, "limit": 5, "kind": "skill"}, "control")
    ]
    assert read.availability == "available"
    assert [entry.component_id for entry in read.entries] == ["comp-one", "c2"]


def test_lessons_preserve_engine_order_and_abstain_when_empty(monkeypatch) -> None:
    wire = _WireModule(
        results={
            "lessons": [
                _lesson(confidence=0.9, what_changed="a"),
                _lesson(confidence=0.3, what_changed="b"),
            ]
        }
    )
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client)
    read = _run(
        NativeLessonRetriever().retrieve(
            context=context,
            task_class="t",
            deficiency="d",
            trajectory_features={"steps": 9.0},
        )
    )
    assert [hit.confidence for hit in read.hits] == [0.9, 0.3]
    assert read.abstained is False
    assert [hit.what_changed for hit in read.hits] == ["a", "b"]

    empty = _WireModule(results={"lessons": []})
    monkeypatch.setitem(sys.modules, WIRE_MODULE, empty.module())
    empty_read = _run(
        NativeLessonRetriever().retrieve(
            context=context,
            task_class="t",
            deficiency="d",
            trajectory_features={},
        )
    )
    assert empty_read.availability == "available"
    assert empty_read.hits == ()
    assert empty_read.abstained is True


def test_evolution_report_projects_the_record(monkeypatch) -> None:
    wire = _WireModule(results={"report": _report()})
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client)
    read = _run(
        NativeEvolutionReportReader().report(context=context, task_ref="task-one")
    )
    assert read.availability == "available"
    assert read.report is not None
    assert read.report.decision_id == "decision-one"
    assert read.report.contrast_digest == CONTRAST_DIGEST
    assert read.report.adopted_version_ref == "ver-a"


@pytest.mark.parametrize(
    "results",
    [
        {"current": None},
        {"current": {"component_id": "comp-one"}},
        {"current": _component_version(valid_to="2025-12-31T00:00:00Z")},
        {"list": "not-a-list"},
        {"lessons": {"unexpected": "shape"}},
        {"lessons": [_lesson(evidence_run_ids=[])]},
        {"report": None},
        {"report": _report(unknown_fact=True)},
    ],
    ids=[
        "null-current",
        "partial-current",
        "invalid-interval",
        "list-not-a-list",
        "lessons-wrong-shape",
        "lesson-without-evidence",
        "null-report",
        "report-unknown-field",
    ],
)
def test_malformed_wire_answers_project_unavailable(monkeypatch, results) -> None:
    wire = _WireModule(results=results)
    monkeypatch.setitem(sys.modules, WIRE_MODULE, wire.module())
    client = _Client()
    context = _context(client=client)
    key = next(iter(results))
    if key == "current":
        read = _run(
            NativeComponentReader().get(
                context=context, component_id="comp-one", as_of=None
            )
        )
        assert read == UNAVAILABLE_COMPONENT_GET
    elif key == "list":
        read = _run(
            NativeComponentReader().list(
                context=context, cursor=None, limit=5, kind=None
            )
        )
        assert read == UNAVAILABLE_COMPONENT_LIST
    elif key == "lessons":
        read = _run(
            NativeLessonRetriever().retrieve(
                context=context, task_class="t", deficiency="d", trajectory_features={}
            )
        )
        assert read == UNAVAILABLE_LESSON_RETRIEVAL
    else:
        read = _run(
            NativeEvolutionReportReader().report(context=context, task_ref="task-one")
        )
        assert read == UNAVAILABLE_EVOLUTION_REPORT
    assert len(client.uses) == 1


def test_engine_refusal_propagates_as_engine_refusal(monkeypatch) -> None:
    from epistemic_graph import EngineResponseError

    from arbora.api.errors import EngineRefusal

    class _FailingModule(SimpleNamespace):
        async def send_current_harness_component(self, client, payload, graph):
            raise EngineResponseError(code="TENANT_MISMATCH", detail="denied")

        def decode_current_harness_component(self, result):
            raise AssertionError("decode must not run on a refused call")

    monkeypatch.setitem(sys.modules, WIRE_MODULE, _FailingModule())
    client = _Client()
    context = _context(client=client)
    with pytest.raises(EngineRefusal) as excinfo:
        _run(
            NativeComponentReader().get(
                context=context, component_id="comp-one", as_of=None
            )
        )
    assert excinfo.value.code == "TENANT_MISMATCH"
