"""Source writes and server-side landing (ARBORA-OPS-R848.2, ARBORA-OPS-R849)."""

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import source as source_ops
from arbora.api.ops import source_landing
from arbora.api.ops.source_landing import FAST_GATES, GateResult
from arbora.api.registry import AuditClass, Confirm, Effect, Idempotency
from tests.api._ops_support import service_context
from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

KEY = "Idempotency-Key"
PLAN = "Arbora-Plan-Ref"
BLOB = {"digest": "sha256:abc", "size": 3}
COMMIT = {
    "repository": "repo-a",
    "branch": "feature",
    "message": "add a file",
    "paths": [{"path": "src/app.py", "op": "add", "mode": 0o100644, "blob": BLOB}],
}
WRITER = ("blob:write", "graph:write")


def _write_ops() -> dict[str, Any]:
    return {op.id: op for op in source_ops.operations() if op.effect != Effect.READ}


@pytest.mark.spec("ARBORA-OPS-R848.2")
def test_write_ops_are_destructive_plan_confirmed_and_identity_audited() -> None:
    ops = _write_ops()
    assert set(ops) == {"source.commit", "source.land", "source.revert"}
    for op in ops.values():
        assert op.effect == Effect.DESTRUCTIVE
        assert op.confirm == Confirm.PLAN
        assert op.idempotency == Idempotency.KEY_REQUIRED
        assert op.audit == AuditClass.IDENTITY_CHAIN
        assert op.scopes == frozenset(WRITER)


@pytest.mark.spec("ARBORA-OPS-R848.2")
@pytest.mark.parametrize(
    "path",
    [
        {"path": "a", "op": "delete", "mode": 0, "blob": BLOB},
        {"path": "a", "op": "add", "mode": 0},
        {"path": "a", "op": "modify", "mode": 0, "blob": BLOB, "renamed_from": "b"},
        {"path": "../a", "op": "add", "mode": 0, "blob": BLOB},
    ],
)
def test_malformed_changesets_are_refused_before_the_engine(path) -> None:
    with pytest.raises(ValidationError):
        source_ops.SourceCommitParams.model_validate({**COMMIT, "paths": [path]})


class _Engine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def submit(self, params: dict[str, Any], _who: str) -> Any:
        self.calls.append(("SubmitChangeset", params))
        if params["request"]["branch"] == "locked":
            raise EngineResponseError("ACCESS_DENIED", "no write grant")
        return {"branch_head": "h2", "changeset": {"id": "c1"}, "replayed": False}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    return confirmable_invocation(monkeypatch)


@pytest.fixture
def env(stack: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    engine = _Engine()
    route_methods(
        client,
        {"SubmitChangeset": engine.submit},
        monkeypatch,
        {"fixture-verified": person("one", extra_scopes=WRITER)},
    )
    return app, engine, stack


def _confirmed(app: Any, params: dict[str, Any], key: str) -> Any:
    preview = _call(app, "source.commit", params, **{KEY: key})
    assert preview.json()["error"]["code"] == "CONFIRMATION_REQUIRED", preview.text
    ref = preview.json()["error"]["details"]["plan_ref"]
    return _call(app, "source.commit", params, **{KEY: key, PLAN: ref})


@pytest.mark.spec("ARBORA-OPS-R848.2")
def test_commit_without_an_approved_plan_makes_zero_engine_calls(env: Any) -> None:
    app, engine, _ = env
    unconfirmed = _call(app, "source.commit", COMMIT, **{KEY: "k1"})
    assert unconfirmed.json()["error"]["code"] == "CONFIRMATION_REQUIRED"
    assert engine.calls == []


@pytest.mark.spec("ARBORA-OPS-R848.2")
def test_confirmed_commit_submits_the_changeset_and_returns_the_receipt(
    env: Any,
) -> None:
    app, engine, _ = env
    receipt = _value(_confirmed(app, COMMIT, "k2"))
    assert receipt["branch_head"] == "h2"
    ((name, params),) = engine.calls
    assert name == "SubmitChangeset"
    request = params["request"]
    assert request["repo"] == "repo-a" and "repository" not in request
    assert request["paths"][0]["blob"] == BLOB


@pytest.mark.spec("ARBORA-OPS-R848.2")
def test_engine_refusals_pass_through_unchanged(env: Any) -> None:
    app, _engine, _ = env
    refused = _confirmed(app, {**COMMIT, "branch": "locked"}, "k3")
    assert refused.json()["error"]["code"] == "ACCESS_DENIED"


@pytest.mark.spec("ARBORA-OPS-R848.2")
@pytest.mark.asyncio
async def test_revert_refuses_unavailable_naming_the_missing_method() -> None:
    op = _write_ops()["source.revert"]
    with pytest.raises(OperationRefused) as caught:
        await source_ops.handle_source_revert(
            service_context(tenant="tenant:one"),
            {"repository": "r", "branch": "b", "changeset": "c"},
            op,
        )
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.details["missing"] == "RevertChangeset"


# --- landing -------------------------------------------------------------


def _gate(gate_id: str, passed: bool = True) -> GateResult:
    return GateResult(
        gate_id=gate_id,
        passed=passed,
        input_digest="in",
        evidence_digest="ev",
        duration_ms=5,
    )


class _Runner:
    def __init__(self, results: list[GateResult] | Exception) -> None:
        self.results = results
        self.calls = 0

    async def run_fast_gates(self, **_kw: Any) -> list[GateResult]:
        self.calls += 1
        if isinstance(self.results, Exception):
            raise self.results
        return self.results


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) | {"RecordGateVerdict"}
    )
    sent: list[dict[str, Any]] = []

    async def send(_context: Any, method: str, params: dict[str, Any], **_kw: Any):
        assert method == "RecordGateVerdict"
        sent.append(params)

    monkeypatch.setattr(source_landing, "engine_send", send)
    return sent


async def _land(runner: Any, **services: Any) -> Any:
    context = service_context(tenant="tenant:one", gate_runner=runner, **services)
    return await source_landing.land_changeset(
        context, repository="r", branch="b", changeset="c1"
    )


@pytest.mark.spec("ARBORA-OPS-R849")
@pytest.mark.asyncio
async def test_all_fast_gates_pass_and_every_verdict_is_recorded(recorded) -> None:
    runner = _Runner([_gate(g) for g in FAST_GATES])
    landed = await _land(runner)
    assert [v["gate_id"] for v in recorded] == list(FAST_GATES)
    assert all(v["verdict"] == "pass" and v["changeset"] == "c1" for v in recorded)
    assert len(landed["verdicts"]) == len(FAST_GATES)


@pytest.mark.spec("ARBORA-OPS-R849")
@pytest.mark.asyncio
async def test_a_failing_gate_refuses_policy_denied_naming_the_gate(recorded) -> None:
    results = [_gate(g, passed=g != "secrets") for g in FAST_GATES]
    with pytest.raises(OperationRefused) as caught:
        await _land(_Runner(results))
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details == {"reason": "fast_gate_failed", "gate": "secrets"}
    assert {v["gate_id"]: v["verdict"] for v in recorded}["secrets"] == "fail"


@pytest.mark.spec("ARBORA-OPS-R849")
@pytest.mark.asyncio
async def test_a_missing_gate_and_a_timeout_both_count_as_failed(recorded) -> None:
    with pytest.raises(OperationRefused) as short:
        await _land(_Runner([_gate("format")]))
    assert short.value.details["reason"] == "fast_gate_failed"
    with pytest.raises(OperationRefused) as late:
        await _land(_Runner(TimeoutError()))
    assert late.value.details == {"reason": "fast_gate_failed", "gate": "timeout"}


@pytest.mark.spec("ARBORA-OPS-R849")
@pytest.mark.asyncio
async def test_landing_needs_a_runner_and_a_bounded_timeout(recorded) -> None:
    with pytest.raises(OperationRefused) as none:
        await source_landing.land_changeset(
            service_context(tenant="t"), repository="r", branch="b", changeset="c"
        )
    assert none.value.code == "UNAVAILABLE"
    runner = _Runner([])
    with pytest.raises(OperationRefused) as bad:
        await _land(runner, source_land_fast_gate_timeout_s=5)
    assert bad.value.code == "INVALID_ARGUMENT" and runner.calls == 0


@pytest.mark.spec("ARBORA-OPS-R849")
@pytest.mark.asyncio
async def test_pinned_engine_refuses_landing_before_any_gate_runs() -> None:
    runner = _Runner([_gate(g) for g in FAST_GATES])
    with pytest.raises(OperationRefused) as caught:
        await _land(runner)
    assert caught.value.code == "UNAVAILABLE"
    assert caught.value.details["missing"] == "RecordGateVerdict"
    assert runner.calls == 0
