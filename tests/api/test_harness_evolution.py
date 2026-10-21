"""Harness evolution report and adoption: registration, admission, refusal
paths (ARBORA-OPS-R068.3, ARBORA-OPS-R068.4).

Wiring tests only: the served round trip is pending the engine release
carrying the contrast-decision record of EG-DECISION-ENGINE-R803.
"""

from __future__ import annotations

import asyncio

import pytest
from eunomia_core import schemas
from pydantic import ValidationError

from arbora.api.invoke import invoke
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import principal_rule
from arbora.api.ops import harness_evolution
from arbora.api.ops.harness_evolution import (
    UNAVAILABLE_EVOLUTION_REPORT,
    AdoptionReceipt,
    EvolutionReport,
    EvolutionReportRead,
    HarnessEvolutionAdoptParams,
    operations,
)
from arbora.api.ops.registry_factory import get_registry
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import Registry, Surface
from tests.api._ops_support import harness_context
from tests.api.test_invoke_contract import (
    FixtureRuntime,
    caller,
    setup_services,
)

REPORT_SERVICE = "harness_evolution_report_reader"
ADOPT_SERVICE = "harness_adoption_runner"


class _ReportReader:
    def __init__(self, read: EvolutionReportRead | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.read = read or UNAVAILABLE_EVOLUTION_REPORT

    async def report(self, **kwargs: object) -> EvolutionReportRead:
        self.calls.append(kwargs)
        return self.read


class _AdoptionRunner:
    def __init__(self, receipt: dict[str, object] | None = None) -> None:
        self.calls: list[tuple[dict[str, object], str]] = []
        self.receipt = receipt or {}

    async def adopt(self, request, *, tenant: str) -> dict[str, object]:
        self.calls.append((request, tenant))
        return self.receipt


def _report(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "decision_id": "decision-one",
        "task_ref": "task-one",
        "task_class": "task:retrieval",
        "conditions_digest": "cd" * 32,
        "contrast_digest": "cf" * 32,
        "probe_trace_ids": ("trace-a", "trace-b"),
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


def _read(
    report: dict[str, object] | None = None, **changes: object
) -> EvolutionReportRead:
    values: dict[str, object] = {"availability": "available", "report": report}
    values.update(changes)
    return EvolutionReportRead.model_validate(values)


def _receipt(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "decision_ref": "decision-one",
        "adopted_version_ref": "ver-a",
        "agent_ref": "agent-one",
        "task_class": "task:retrieval",
        "component_version_refs": ("ver-a",),
        "contrast_ref": "contrast-one",
    }
    values.update(changes)
    return values


def _ops():
    return {op.id: op for op in operations()}


def test_evolution_ops_are_registered_with_decide_commit_authority() -> None:
    ops = _ops()
    assert set(ops) == {"harness.evolution.report", "harness.evolution.adopt"}
    report = ops["harness.evolution.report"]
    assert report.verb.value == "ask"
    assert report.scopes == frozenset({"kg:read"})
    assert report.effect.value == "read"
    adopt = ops["harness.evolution.adopt"]
    assert adopt.verb.value == "act"
    assert adopt.effect.value == "write"
    assert adopt.scopes == frozenset({"decide:commit"})
    assert adopt.executor_scopes == frozenset({"decide:commit"})
    assert adopt.executor.value == "service"
    assert adopt.principals.value == "service_only"
    assert adopt.subject is not None and adopt.subject.source.value == "caller_tenant"
    assert adopt.audit.value == "event"
    assert adopt.idempotency.value == "none"
    assert adopt.binding.handler == (
        "arbora.api.ops.harness_evolution.handle_harness_evolution_adopt"
    )
    registry = get_registry()
    for op_id in ops:
        assert op_id in {op.id for op in registry}


def test_adopt_refuses_a_human_scoped_token() -> None:
    """The decide-commit pattern: only a service principal may adopt."""
    adopt = _ops()["harness.evolution.adopt"]
    human = caller(
        principal_kind="human", effective_scopes=frozenset({"decide:commit"})
    )
    assert principal_rule(adopt, human) is not None, (
        "a human-scoped token must be refused"
    )
    service = caller(
        principal_kind="service", effective_scopes=frozenset({"decide:commit"})
    )
    assert principal_rule(adopt, service) is None


async def test_report_absent_reader_refuses_unavailable(op_by_id) -> None:
    context = harness_context(services={})
    with pytest.raises(OperationRefused) as excinfo:
        await harness_evolution.handle_harness_evolution_report(
            context, {"task_ref": "task-one"}, _ops()["harness.evolution.report"]
        )
    assert excinfo.value.code == "UNAVAILABLE"
    assert excinfo.value.details["provenance"] == (
        f"harness.admission:harness.evolution.report:{REPORT_SERVICE}-uncomposed"
    )


async def test_report_denied_scope_refuses_before_the_reader(op_by_id) -> None:
    reader = _ReportReader()
    context = harness_context(services={REPORT_SERVICE: reader}, scopes=())
    with pytest.raises(OperationRefused) as excinfo:
        await harness_evolution.handle_harness_evolution_report(
            context, {"task_ref": "task-one"}, _ops()["harness.evolution.report"]
        )
    assert excinfo.value.code == "SCOPE_REQUIRED"
    assert reader.calls == []


async def test_report_carries_decision_digest_and_adopted_ref(op_by_id) -> None:
    reader = _ReportReader(read=_read(_report()))
    context = harness_context(services={REPORT_SERVICE: reader})
    result = await harness_evolution.handle_harness_evolution_report(
        context, {"task_ref": "task-one"}, _ops()["harness.evolution.report"]
    )
    assert reader.calls == [{"context": context, "task_ref": "task-one"}]
    value = result["value"]
    assert value["availability"] == "available"
    assert value["report"]["decision_id"] == "decision-one"
    assert value["report"]["contrast_digest"] == "cf" * 32
    assert value["report"]["adopted_version_ref"] == "ver-a"


async def test_report_malformed_answer_is_unavailable(op_by_id) -> None:
    reader = _ReportReader()
    reader.read = EvolutionReportRead.model_construct(
        availability="available", report=_report(unknown_fact=True)
    )
    result = await harness_evolution.handle_harness_evolution_report(
        harness_context(services={REPORT_SERVICE: reader}),
        {"task_ref": "task-one"},
        _ops()["harness.evolution.report"],
    )
    assert result["value"] == UNAVAILABLE_EVOLUTION_REPORT.model_dump(mode="json")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"task_ref": ""},
        {"task_ref": "task-one", "tenant": "x"},
    ],
)
async def test_report_malformed_params_never_reach_the_reader(op_by_id, params) -> None:
    reader = _ReportReader()
    context = harness_context(services={REPORT_SERVICE: reader})
    with pytest.raises(ValidationError):
        await harness_evolution.handle_harness_evolution_report(
            context, params, _ops()["harness.evolution.report"]
        )
    assert reader.calls == []


async def test_adopt_absent_runner_refuses_unavailable(op_by_id) -> None:
    context = harness_context(
        services={}, scopes=("decide:commit",), principal_kind="service"
    )
    with pytest.raises(OperationRefused) as excinfo:
        await harness_evolution.handle_harness_evolution_adopt(
            context, _receipt(), _ops()["harness.evolution.adopt"]
        )
    assert excinfo.value.code == "UNAVAILABLE"
    assert excinfo.value.details["reason"] == "HARNESS_READER_UNAVAILABLE"


async def test_adopt_rejects_a_malformed_receipt(op_by_id) -> None:
    runner = _AdoptionRunner(receipt=_receipt(unexpected_field=True))
    context = harness_context(
        services={ADOPT_SERVICE: runner},
        scopes=("decide:commit",),
        principal_kind="service",
    )
    with pytest.raises(OperationRefused) as excinfo:
        await harness_evolution.handle_harness_evolution_adopt(
            context, _receipt(), _ops()["harness.evolution.adopt"]
        )
    assert excinfo.value.code == "UNAVAILABLE"
    assert excinfo.value.details["reason"] == "adoption receipt malformed"


async def test_adopt_forwards_the_typed_request_to_the_runner(op_by_id) -> None:
    request = _receipt()
    runner = _AdoptionRunner(receipt=request)
    context = harness_context(
        services={ADOPT_SERVICE: runner},
        scopes=("decide:commit",),
        principal_kind="service",
    )
    result = await harness_evolution.handle_harness_evolution_adopt(
        context, request, _ops()["harness.evolution.adopt"]
    )
    forwarded = HarnessEvolutionAdoptParams.model_validate(request).model_dump(
        mode="json"
    )
    assert runner.calls == [(forwarded, "tenant-a")]
    assert result["value"] == AdoptionReceipt.model_validate(request).model_dump(
        mode="json"
    )


def test_pipeline_refuses_human_adopt_before_any_runner_call() -> None:
    """A human-scoped token cannot invoke the adoption; the runner is never
    reached, so no adoption is stored."""
    runner = _AdoptionRunner()
    services, runtime, audit, _, _ = setup_services(
        registry=Registry([*_ops().values()]),
        external_effect_operations=frozenset(),
    )
    runtime.bindings = {ADOPT_SERVICE: runner}
    runtime.service_scopes = frozenset({"decide:commit"})
    _resolve_handlers(runtime)
    human = caller(
        principal_kind="human",
        effective_scopes=frozenset({"decide:commit"}),
    )
    out = _run(
        invoke(
            "harness.evolution.adopt",
            _receipt(),
            human,
            Surface.HTTP,
            services=services,
        )
    )
    assert out.code == "PRINCIPAL_NOT_ALLOWED"
    assert runner.calls == []
    assert audit.events == []
    assert runtime.calls == []


def test_pipeline_denied_scope_sees_neither_contrast_nor_record() -> None:
    """A caller denied the read scope receives the refusal and nothing
    else: the report reader is never called."""
    reader = _ReportReader()
    services, runtime, audit, _, _ = setup_services(
        registry=Registry([*_ops().values()]),
        external_effect_operations=frozenset(),
    )
    runtime.bindings = {REPORT_SERVICE: reader}
    _resolve_handlers(runtime)
    denied = caller(
        principal_kind="human",
        effective_scopes=frozenset(),
    )
    out = _run(
        invoke(
            "harness.evolution.report",
            {"task_ref": "task-one"},
            denied,
            Surface.HTTP,
            services=services,
        )
    )
    assert out.code == "SCOPE_REQUIRED"
    assert reader.calls == []
    assert runtime.calls == []
    assert audit.events == []


def test_pipeline_policy_denial_hides_contrast_and_record() -> None:
    """Even a scoped caller denied by the policy gate sees only the
    refusal: the report reader is never called."""
    reader = _ReportReader()

    class _DenyingPDP:
        async def bulk_check(self, requests):
            return [
                schemas.CheckResponse(allowed=False, reason="policy") for _ in requests
            ]

    gate = PolicyGate("embedded", _DenyingPDP())
    services, runtime, audit, _, _ = setup_services(
        registry=Registry([*_ops().values()]),
        policy_gate=gate,
        external_effect_operations=frozenset(),
    )
    runtime.bindings = {REPORT_SERVICE: reader}
    _resolve_handlers(runtime)
    scoped = caller(
        principal_kind="human",
        effective_scopes=frozenset({"kg:read"}),
    )
    out = _run(
        invoke(
            "harness.evolution.report",
            {"task_ref": "task-one"},
            scoped,
            Surface.HTTP,
            services=services,
        )
    )
    assert out.code == "POLICY_DENIED"
    assert reader.calls == []
    assert runtime.calls == []


def test_pipeline_adopt_refuses_until_the_adoption_owner_is_composed() -> None:
    """A governed write needs a durable replay owner: until the engine's
    adoption record (EG-DECISION-ENGINE-R803) composes one, the scoped
    service call is refused after the subject read, before the runner
    and the audit are touched."""
    runner = _AdoptionRunner(receipt=_receipt())
    services, runtime, audit, _, _ = setup_services(
        registry=Registry([*_ops().values()]),
        external_effect_operations=frozenset(),
    )
    runtime.bindings = {ADOPT_SERVICE: runner}
    runtime.service_scopes = frozenset({"decide:commit"})
    _resolve_handlers(runtime)
    service = caller(
        principal_kind="service",
        effective_scopes=frozenset({"decide:commit"}),
    )
    out = _run(
        invoke(
            "harness.evolution.adopt",
            _receipt(),
            service,
            Surface.HTTP,
            services=services,
            idempotency_key="adopt:key:one",
        )
    )
    assert out.code == "UNAVAILABLE"
    assert out.details == {"reason": "durable effect owner unavailable"}
    assert [subject for _, subject in runtime.subject_calls] == ["tenant:one"]
    assert runner.calls == []
    assert runtime.calls == []
    assert audit.events == []


def test_pipeline_service_adoption_stores_one_adoption_with_audit() -> None:
    """With the adoption's replay owner composed (here the effect journal),
    a service call with the exact scope and an admitted subject stores
    one adoption and reserves its audit record."""
    runner = _AdoptionRunner(receipt=_receipt())
    services, runtime, audit, journal, _ = setup_services(
        registry=Registry([*_ops().values()]),
        external_effect_operations=frozenset({"harness.evolution.adopt"}),
    )
    runtime.bindings = {ADOPT_SERVICE: runner}
    runtime.service_scopes = frozenset({"decide:commit"})
    _resolve_handlers(runtime)
    service = caller(
        principal_kind="service",
        effective_scopes=frozenset({"decide:commit"}),
    )
    out = _run(
        invoke(
            "harness.evolution.adopt",
            _receipt(),
            service,
            Surface.HTTP,
            services=services,
            idempotency_key="adopt:key:one",
        )
    )
    assert out.code == "OK"
    assert runner.calls == [
        (
            HarnessEvolutionAdoptParams.model_validate(_receipt()).model_dump(
                mode="json"
            ),
            "tenant:one",
        )
    ]
    bound, subject = runtime.subject_calls[0]
    assert subject == "tenant:one"
    assert (bound.principal, bound.tenant) == (service.principal, service.tenant)
    assert len(audit.events) == 2  # reservation plus linked outcome
    assert len(journal.rows) == 1
    assert journal.completions == list(journal.rows)
    _, reservation = next(iter(journal.rows.values()))
    assert reservation.state == "completed"


def test_pipeline_service_without_subject_read_is_denied() -> None:
    """The caller read check on the subject runs before the commit."""
    runner = _AdoptionRunner(receipt=_receipt())
    services, runtime, audit, _, _ = setup_services(
        registry=Registry([*_ops().values()]),
        external_effect_operations=frozenset(),
    )
    runtime.bindings = {ADOPT_SERVICE: runner}
    runtime.service_scopes = frozenset({"decide:commit"})
    runtime.subject_allowed = False
    _resolve_handlers(runtime)
    service = caller(
        principal_kind="service",
        effective_scopes=frozenset({"decide:commit"}),
    )
    out = _run(
        invoke(
            "harness.evolution.adopt",
            _receipt(),
            service,
            Surface.HTTP,
            services=services,
            idempotency_key="adopt:key:two",
        )
    )
    assert out.code == "SUBJECT_ACCESS_DENIED"
    assert runner.calls == []
    assert runtime.calls == []


def _run(coro):
    return asyncio.run(coro)


def _resolve_handlers(runtime: FixtureRuntime) -> None:
    """Point the fixture runtime's dispatch at the real composite handlers.

    The stub dispatch would hide handler behavior; these pipeline tests
    must prove the real handlers (and only they) run for an authorized
    caller, and never run for a refused one.
    """
    from importlib import import_module

    async def dispatch(op, params, context):
        runtime.calls.append((op, params, context))
        module_name, function_name = op.binding.handler.rsplit(".", 1)
        handler = getattr(import_module(module_name), function_name)
        return await handler(context, params, op)

    runtime.dispatch = dispatch  # type: ignore[method-assign]


def test_evolution_report_model_refuses_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        EvolutionReport.model_validate(_report(sneaky=True))
    with pytest.raises(ValidationError):
        EvolutionReport.model_validate(_report(abstain_reason="feeling-uninspired"))
