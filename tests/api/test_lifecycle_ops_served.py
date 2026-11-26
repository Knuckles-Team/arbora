"""Lifecycle operations over the engine's delivery records (ARBORA-OPS-R853)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import require_scopes
from arbora.api.ops import lifecycle
from arbora.api.registry import Effect
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble, context

_OPS = {op.id: op for op in lifecycle.operations()}
_DIGEST = "sha256:" + "a" * 64
_METHODS = (
    "FileDefect",
    "TransitionDefect",
    "RecordRelease",
    "RecordDeployment",
    "SpecStatus",
    "LifecycleTrace",
)


def _pinned_has_all() -> bool:
    from medulla.generated import METHOD_IDS

    return all(method in METHOD_IDS for method in (*_METHODS, "TriageDefect"))


@pytest.mark.spec("ARBORA-OPS-R853.1")
def test_registry_lists_lifecycle_operations() -> None:
    assert sorted(_OPS) == [
        "defect.file",
        "defect.transition",
        "defect.triage",
        "deployment.record",
        "lifecycle.trace",
        "release.record",
        "spec.status",
    ]
    for op_id in ("spec.status", "lifecycle.trace"):
        assert _OPS[op_id].effect is Effect.READ
        assert _OPS[op_id].scopes == frozenset({"kg:read"})
    for op_id, op in _OPS.items():
        if op_id not in ("spec.status", "lifecycle.trace"):
            assert op.effect is Effect.WRITE, op_id
            assert op.scopes == frozenset({"lifecycle:write"}), op_id
            assert op_id in lifecycle.NATIVE_IDEMPOTENCY


@pytest.mark.spec("ARBORA-OPS-R853.1")
@pytest.mark.skipif(_pinned_has_all(), reason="the pin carries the lifecycle methods")
@pytest.mark.parametrize(
    ("handler", "params", "missing"),
    [
        (
            "handle_defect_file",
            {
                "repo": "r",
                "intake": "verdict",
                "title": "t",
                "source_record_id": "v1",
                "observed_at": 1,
                "text": "x",
            },
            "FileDefect",
        ),
        ("handle_defect_triage", {"defect_id": "d1"}, "TriageDefect"),
        (
            "handle_deployment_record",
            {
                "environment": "e",
                "workload": "w",
                "image_digest": _DIGEST,
                "started_at": 1,
                "finished_at": 2,
                "outcome": "succeeded",
            },
            "RecordDeployment",
        ),
        ("handle_spec_status", {"repo": "r", "rev": "abc"}, "SpecStatus"),
        ("handle_lifecycle_trace", {"anchor": "a", "depth": 2}, "LifecycleTrace"),
    ],
)
async def test_a_pinned_engine_without_the_method_refuses_naming_it(
    handler: str, params: dict[str, Any], missing: str
) -> None:
    from medulla.generated import METHOD_IDS

    if missing in METHOD_IDS:
        pytest.skip("the pin carries this method")
    op_id = handler.removeprefix("handle_").replace("_", ".", 1)
    with pytest.raises(OperationRefused) as refused:
        await getattr(lifecycle, handler)(context(), params, _OPS[op_id])
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == missing


@pytest.mark.spec("ARBORA-OPS-R853", "ARBORA-OPS-R853.2")
async def test_defect_to_deployment_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = EngineDouble(monkeypatch)
    nodes: list[str] = []

    def file_defect(p: dict[str, Any]) -> Any:
        nodes.append("defect")
        return {"defect_id": "d1", "created": True}

    def release(p: dict[str, Any]) -> Any:
        nodes.append("release")
        return {"release_id": "rel1", "replayed": False}

    def deployment(p: dict[str, Any]) -> Any:
        nodes.append("deployment")
        return {"deployment_id": "dep1", "release_ref": "rel1"}

    engine.serve("FileDefect", file_defect)
    engine.serve("TriageDefect", lambda p: {"defect_id": p["request"]["defect_id"]})
    engine.serve("TransitionDefect", lambda p: {"status": p["request"]["to"]})
    engine.serve("RecordRelease", release)
    engine.serve("RecordDeployment", deployment)
    engine.serve("SpecStatus", lambda p: {"rows": [{"id": "R1", "state": "VERIFIED"}]})
    engine.serve(
        "LifecycleTrace",
        lambda p: {
            "records": [
                {"kind": "requirement", "id": p["request"]["anchor"]},
                {"kind": "deployment", "id": "dep1"},
            ]
        },
    )
    engine.install(pin_lag)
    ctx = context()

    filed = await lifecycle.handle_defect_file(
        ctx,
        {
            "repo": "r",
            "intake": "verdict",
            "title": "gate failed",
            "source_record_id": "verdict-1",
            "observed_at": 5,
            "text": "gate failed",
        },
        _OPS["defect.file"],
    )
    triaged = await lifecycle.handle_defect_triage(
        ctx, {"defect_id": "d1"}, _OPS["defect.triage"]
    )
    moved = await lifecycle.handle_defect_transition(
        ctx,
        {"repo": "r", "defect_id": "d1", "to": "fixed", "fix_changeset": "c1"},
        _OPS["defect.transition"],
    )
    await lifecycle.handle_release_record(
        ctx,
        {
            "repo": "r",
            "version": "1.0.0",
            "git_ref": "refs/tags/v1.0.0",
            "changeset_head": "c1",
            "published_at": 9,
        },
        _OPS["release.record"],
    )
    await lifecycle.handle_deployment_record(
        ctx,
        {
            "environment": "prod",
            "workload": "api",
            "image_digest": _DIGEST,
            "started_at": 10,
            "finished_at": 11,
            "outcome": "succeeded",
        },
        _OPS["deployment.record"],
    )
    status = await lifecycle.handle_spec_status(
        ctx, {"repo": "r", "rev": "c1"}, _OPS["spec.status"]
    )
    trace = await lifecycle.handle_lifecycle_trace(
        ctx, {"anchor": "R1", "depth": 4}, _OPS["lifecycle.trace"]
    )

    assert [m for m, _, _ in engine.sent] == [
        "FileDefect",
        "TriageDefect",
        "TransitionDefect",
        "RecordRelease",
        "RecordDeployment",
        "SpecStatus",
        "LifecycleTrace",
    ]
    assert filed["value"]["defect_id"] == "d1" and triaged["value"]["defect_id"] == "d1"
    assert moved["value"]["status"] == "fixed"
    assert status["value"]["rows"][0]["state"] == "VERIFIED"
    assert [r["kind"] for r in trace["value"]["records"]] == [
        "requirement",
        "deployment",
    ]
    assert nodes == ["defect", "release", "deployment"]
    writes = {m: idem for m, _, idem in engine.sent}
    assert writes["FileDefect"] and writes["RecordDeployment"]
    assert not writes["SpecStatus"] and not writes["LifecycleTrace"]
    # The triage carries the verified tenant, never a request field.
    triage = next(p for m, p, _ in engine.sent if m == "TriageDefect")
    assert triage["request"]["tenant_id"] == "tenant:one"
    # A caller without the write scope is refused before any handler runs.
    caller = SimpleNamespace(effective_scopes=frozenset({"kg:read"}))
    refused = require_scopes(_OPS["defect.transition"], caller)
    assert refused is not None and refused.code == "SCOPE_REQUIRED"
    assert refused.details == {"missing_scopes": ["lifecycle:write"]}
    assert require_scopes(_OPS["lifecycle.trace"], caller) is None


@pytest.mark.spec("ARBORA-OPS-R853.1")
async def test_triage_passes_the_engine_codes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)

    def refuse(p: dict[str, Any]) -> Any:
        raise EngineRefusal("DEFECT_CLOSED")

    engine.serve("TriageDefect", refuse)
    engine.install(pin_lag)
    with pytest.raises(EngineRefusal) as refused:
        await lifecycle.handle_defect_triage(
            context(), {"defect_id": "d1"}, _OPS["defect.triage"]
        )
    assert refused.value.code == "DEFECT_CLOSED"
