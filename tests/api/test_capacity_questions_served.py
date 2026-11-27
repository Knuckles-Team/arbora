"""The ingest shortfall and run adjustment questions, and the index grant clamp.

ARBORA-CAPACITY-R003, R001.2 and R753. The engine is a recorded ``Decide`` answer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import capacity_questions
from arbora.api.ops._question import Q32_ONE
from arbora.api.ops.registry_factory import get_registry
from arbora.ingest import service
from tests.api._lane_double import EngineDouble, context


def _advisory(*options: str) -> dict[str, Any]:
    return {
        "outcome": {
            "outcome": "advisory",
            "scores": [
                {"option_id": name, "score": {"value": Q32_ONE}} for name in options
            ],
        }
    }


def _patch_decide(
    monkeypatch: pytest.MonkeyPatch, record: dict[str, Any], asked: list[Any]
) -> None:
    async def decide(ctx: Any, kind: str, request: Any, *, family_codes: Any) -> Any:
        asked.append((kind, request))
        return record

    async def schema(ctx: Any, tenant: str) -> dict[str, Any]:
        return {"component_id": "x", "kind": "feature_schema", "definition_digest": "d"}

    monkeypatch.setattr(capacity_questions, "decide_question", decide)
    monkeypatch.setattr("arbora.api.ops.plans._feature_schema", schema)


def _candidate(item_id: str) -> dict[str, Any]:
    """An engine ``PurgeCandidate`` (MEDULLA-DECISION-ENGINE-R826)."""
    return {
        "item_id": item_id,
        "terminal": True,
        "leased": False,
        "authoritative": False,
        "source_receipt": True,
        "decision_receipt": True,
        "last_source_copy": False,
        "active_work": False,
    }


def _assert_engine_shape(request: dict[str, Any]) -> None:
    """The one ``allocation`` option and the integer budget the engine family reads."""
    by_name = {p["name"]: p["value"] for p in request["params"]}
    assert by_name["budget"] == {"type": "int", "value": 10}
    assert "allocation" in json.dumps(request)
    assert "goal:g1" not in json.dumps(request)


@pytest.mark.spec("ARBORA-CAPACITY-R003")
async def test_below_at_above_threshold_and_refusal_reach_the_engine_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    window = {"papers": 900, "target": 2000}
    client = EngineDouble(MeasureIngestThroughput=lambda params: window)
    ctx = context(client, monkeypatch)
    asked: list[Any] = []
    _patch_decide(monkeypatch, _advisory("goal:g1", "purge:p1", "purge:p2"), asked)
    params = {
        "corpus": "papers",
        "window_end_ms": 5,
        "goals": [
            {"goal_id": "g1", "cost": 4, "expected_papers": 8},
            {"goal_id": "g2", "cost": 9, "expected_papers": 1},
        ],
        "budget": 10,
        "purge_candidates": [_candidate("p1"), _candidate("p2")],
    }
    answered = await capacity_questions.handle_ingest_shortfall_get(ctx, params, None)  # type: ignore[arg-type]
    assert answered["value"] == {
        "window": window,
        "goals": ["g1"],
        "purge": ["p1", "p2"],
    }
    kind, request = asked[0]
    assert kind == "ingest_shortfall"
    _assert_engine_shape(request)
    assert {p["name"] for p in request["params"]} == {
        "window",
        "goals",
        "budget",
        "purge_candidates",
    }
    # At or above target the engine funds and purges nothing: advisory is empty.
    _patch_decide(monkeypatch, _advisory(), asked)
    quiet = await capacity_questions.handle_ingest_shortfall_get(ctx, params, None)  # type: ignore[arg-type]
    assert quiet["value"]["goals"] == [] and quiet["value"]["purge"] == []
    assert get_registry().get("capacity.ingest_shortfall.get") is not None


@pytest.mark.spec("ARBORA-CAPACITY-R003")
async def test_a_family_refusal_keeps_the_engine_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops._question import FamilyRefusal

    client = EngineDouble(MeasureIngestThroughput=lambda params: {})
    ctx = context(client, monkeypatch)

    async def refuse(*a: Any, **k: Any) -> Any:
        raise FamilyRefusal("BUDGET_INVALID")

    async def schema(ctx: Any, tenant: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(capacity_questions, "decide_question", refuse)
    monkeypatch.setattr("arbora.api.ops.plans._feature_schema", schema)
    params = {
        "corpus": "papers",
        "window_end_ms": 5,
        "goals": [{"goal_id": "g1", "cost": 1, "expected_papers": 1}],
        "budget": 0,
        "purge_candidates": [],
    }
    with pytest.raises(OperationRefused) as refused:
        await capacity_questions.handle_ingest_shortfall_get(ctx, params, None)  # type: ignore[arg-type]
    assert refused.value.code == "POLICY_DENIED"
    assert refused.value.details == {"engine_code": "BUDGET_INVALID"}


@pytest.mark.spec("ARBORA-CAPACITY-R001.2")
async def test_the_run_adjustment_record_is_the_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = context(EngineDouble(), monkeypatch)
    sealed = {**_advisory("adjustment"), "record_id": "rec-1"}
    asked: list[Any] = []
    _patch_decide(monkeypatch, sealed, asked)
    answered = await capacity_questions.handle_run_adjustment_get(
        ctx,
        {"run_id": "run-9"},
        None,  # type: ignore[arg-type]
    )
    assert answered["value"]["record"]["record_id"] == "rec-1"
    kind, request = asked[0]
    assert kind == "run_adjustment"
    assert [p["name"] for p in request["params"]] == ["run_id"]
    assert get_registry().get("capacity.run_adjustment.get") is not None


@pytest.mark.spec("ARBORA-CAPACITY-R001.2")
async def test_a_mid_run_widening_is_the_engine_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops._question import FamilyRefusal

    ctx = context(EngineDouble(), monkeypatch)

    async def refuse(*a: Any, **k: Any) -> Any:
        raise FamilyRefusal("WIDEN_MID_RUN")

    async def schema(ctx: Any, tenant: str) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(capacity_questions, "decide_question", refuse)
    monkeypatch.setattr("arbora.api.ops.plans._feature_schema", schema)
    with pytest.raises(OperationRefused) as refused:
        await capacity_questions.handle_run_adjustment_get(
            ctx,
            {"run_id": "run-9"},
            None,  # type: ignore[arg-type]
        )
    assert refused.value.details == {"engine_code": "WIDEN_MID_RUN"}


@pytest.mark.spec("ARBORA-CAPACITY-R753")
async def test_index_grant_is_the_host_grant_clamped_to_the_engine_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(runtime_config, "resource_grants_max_working_bytes", 8_000)

    async def knob() -> int:
        return 5_000

    bound = SimpleNamespace(services={"index_knob_reader": knob})
    assert await service.resource_grant_bytes(bound) == 5_000
    large = SimpleNamespace(services={"index_knob_reader": _constant(9_000)})
    assert await service.resource_grant_bytes(large) == 8_000
    unread = SimpleNamespace(services={})
    assert await service.resource_grant_bytes(unread) == 8_000


def _constant(value: int) -> Any:
    async def read() -> int:
        return value

    return read


def _engine_pages(monkeypatch: pytest.MonkeyPatch, pages: list[dict[str, Any]]) -> list:
    """Serve ``Introspect`` pages as the engine and record each request."""
    from arbora.api.ops import _engine
    from arbora.pin_lag import medulla as pin

    seen: list[tuple[str, dict[str, Any]]] = []

    async def send(context: Any, method: str, params: dict[str, Any], **_: Any) -> Any:
        seen.append((method, dict(params)))
        return pages[len(seen) - 1]

    monkeypatch.setattr(_engine, "engine_send", send)
    monkeypatch.setattr(pin, "require_methods", lambda *names: None)
    monkeypatch.setattr(pin, "require_enum_member", lambda model, value: None)
    return seen


@pytest.mark.spec("ARBORA-CAPACITY-R753")
async def test_the_engine_knob_is_read_from_introspect_knobs_across_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(runtime_config, "resource_grants_max_working_bytes", 8_000)
    seen = _engine_pages(
        monkeypatch,
        [
            {"rows": [{"key": "a.knob", "value": 1}], "next_cursor": "a.knob"},
            {
                "rows": [{"key": service.INDEX_KNOB, "value": 5_000}],
                "next_cursor": None,
            },
        ],
    )
    unbound = SimpleNamespace(services={})
    assert await service.resource_grant_bytes(unbound) == 5_000
    assert [method for method, _ in seen] == ["Introspect", "Introspect"]
    assert seen[0][1]["section"] == "knobs"
    assert seen[1][1]["cursor"] == "a.knob"


@pytest.mark.spec("ARBORA-CAPACITY-R753")
async def test_a_knob_above_the_host_grant_leaves_the_host_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.core.settings import runtime_config

    monkeypatch.setattr(runtime_config, "resource_grants_max_working_bytes", 8_000)
    _engine_pages(
        monkeypatch,
        [{"rows": [{"key": service.INDEX_KNOB, "value": 1_073_741_824}]}],
    )
    assert await service.resource_grant_bytes(SimpleNamespace(services={})) == 8_000


@pytest.mark.spec("ARBORA-CAPACITY-R753")
async def test_a_pinned_engine_without_the_knobs_section_leaves_the_host_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops import _engine
    from arbora.core.settings import runtime_config
    from arbora.pin_lag import medulla as pin
    from arbora.pin_lag.unavailable import PinLagUnavailable

    def lacking(model: str, value: str) -> None:
        raise PinLagUnavailable(f"{model}.{value}")

    async def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no engine call may be made without the section")

    monkeypatch.setattr(runtime_config, "resource_grants_max_working_bytes", 8_000)
    monkeypatch.setattr(pin, "require_methods", lambda *names: None)
    monkeypatch.setattr(pin, "require_enum_member", lacking)
    monkeypatch.setattr(_engine, "engine_send", never)
    assert await service.resource_grant_bytes(SimpleNamespace(services={})) == 8_000
