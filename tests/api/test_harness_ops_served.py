"""The harness component and improvement library operations (ARBORA-OPS-R068)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.pipeline_services import uncovered_write_operations
from arbora.api.ops import harness
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Effect, PrincipalRule
from tests.api._lane_double import EngineDouble, context

OPS = {op.id: op for op in harness.operations()}


def _rows(*ids: str) -> dict[str, object]:
    return {"Rows": {"columns": [], "rows": [{"id": i} for i in ids], "warnings": []}}


@pytest.mark.spec("ARBORA-OPS-R068.1")
@pytest.mark.asyncio
async def test_components_list_and_get_served(monkeypatch: pytest.MonkeyPatch) -> None:
    client = EngineDouble(Uql=_rows("c1", "c2", "c3"))
    ctx = context(client, monkeypatch)
    page = await harness.handle_components_list(
        ctx,
        {"limit": 2, "cursor": "2", "kind": "skill"},
        OPS["harness.components.list"],
    )
    assert [c["id"] for c in page["value"]["components"]] == ["c1", "c2"]
    assert page["value"]["next_cursor"] == "4"
    text = client.sent[0][1]["text"]
    assert "HarnessComponent" in text and "SKIP 2" in text and "LIMIT 3" in text
    assert "skill" not in text  # a value is bound, never spliced into the text
    one = await harness.handle_components_get(
        ctx, {"component_id": "c1"}, OPS["harness.components.get"]
    )
    assert one["value"]["component"]["id"] == "c1"
    absent = context(EngineDouble(Uql=_rows()), monkeypatch)
    with pytest.raises(OperationRefused) as refused:
        await harness.handle_components_get(
            absent, {"component_id": "nope"}, OPS["harness.components.get"]
        )
    assert refused.value.code == "NOT_FOUND"
    for op_id in ("harness.components.list", "harness.components.get"):
        assert OPS[op_id].effect is Effect.READ and OPS[op_id].scopes == {"kg:read"}


@pytest.mark.spec("ARBORA-OPS-R068.2")
@pytest.mark.asyncio
async def test_lessons_retrieve_ranks_by_confidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lessons = [{"confidence": 0.9, "what_changed": "a"}, {"confidence": 0.4}]
    client = EngineDouble(DecisionLog={"lessons": lessons, "abstain_reason": None})
    ctx = context(client, monkeypatch)
    params = {"task_class": "t", "deficiency": "d", "as_of_ms": 5}
    answer = await harness.handle_lessons_retrieve(
        ctx, params, OPS["harness.lessons.retrieve"]
    )
    assert answer["value"]["lessons"] == lessons  # the engine order is kept
    op = client.sent[0][1]["op"]
    assert op["op"] == "harness_lessons" and op["tenant_id"] == "tenant-a"
    empty = context(EngineDouble(DecisionLog={"lessons": []}), monkeypatch)
    abstained = await harness.handle_lessons_retrieve(
        empty, params, OPS["harness.lessons.retrieve"]
    )
    assert abstained["value"] == {"lessons": [], "abstain_reason": "NoLessons"}


@pytest.mark.spec("ARBORA-OPS-R068.3")
@pytest.mark.asyncio
async def test_evolution_report_denial_hides_both(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both = context(EngineDouble(Uql=_rows("x")), monkeypatch)
    seen = await harness.handle_evolution_report(
        both, {"task_ref": "t1"}, OPS["harness.evolution.report"]
    )
    assert set(seen["value"]) == {"contrast", "decision"}
    # The engine hides a read the caller may not make; one hidden read hides both.
    answers = iter([_rows("x"), _rows()])
    half = context(EngineDouble(Uql=lambda _p: next(answers)), monkeypatch)
    with pytest.raises(OperationRefused) as refused:
        await harness.handle_evolution_report(
            half, {"task_ref": "t1"}, OPS["harness.evolution.report"]
        )
    assert refused.value.code == "NOT_FOUND"
    assert "contrast" not in str(refused.value.details)


@pytest.mark.spec("ARBORA-OPS-R068.4")
@pytest.mark.asyncio
async def test_adopt_refuses_human_token(monkeypatch: pytest.MonkeyPatch) -> None:
    adopt = OPS["harness.evolution.adopt"]
    assert adopt.principals is PrincipalRule.SERVICE_ONLY
    assert adopt.executor_scopes == {"decide:commit"}
    assert adopt.idempotency.name == "KEY_REQUIRED" and adopt.effect is Effect.WRITE
    assert "harness.evolution.adopt" not in uncovered_write_operations(get_registry())
    write = {
        "adopted_version_ref": "v2",
        "agent_ref": "a",
        "changed_component_kind": "skill",
        "conditions_digest": "d1",
        "confidence_ppm": 5,
        "decision_record_digest": "d2",
        "deficiency": "x",
        "situation": "s",
        "task_class": "tc",
        "task_ref": "t",
        "what_changed": "w",
    }
    client = EngineDouble(DecisionLog={"adopted_harness_ref": "h1", "replayed": False})
    ctx = context(client, monkeypatch)
    stored = await harness.handle_evolution_adopt(ctx, {"write": write}, adopt)
    assert stored["value"]["adopted_harness_ref"] == "h1"
    method, sent, _graph, key = client.sent[0]
    assert (method, key) == ("DecisionLog", "key-1")
    assert sent["op"]["op"] == "harness_adoption"
    with pytest.raises(OperationRefused) as refused:
        await harness.handle_evolution_adopt(
            ctx, {"write": {**write, "decision_record_digest": " "}}, adopt
        )
    assert refused.value.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-OPS-R068")
def test_harness_operations_are_registered_with_the_registry() -> None:
    served = {op.id for op in get_registry() if op.id.startswith("harness.")}
    assert served == set(OPS)
