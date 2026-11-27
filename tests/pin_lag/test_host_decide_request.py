"""The host port sends a full ``DecideRequest``, wrapped once (ARBORA-HOST-R001.2.5).

The harness port carries only ``(kind, payload)``. The host adds the run's verified
tenant, the question id, the pinned feature schema and the candidate set, and sends the
engine's ``MethodDecideParams {request}`` through ``send_decide``'s wrapper. A pinned
engine that does not list the question refuses ``UNAVAILABLE`` naming it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Literal

import pytest

from arbora.agent_host import host_port_adapters as adapters
from arbora.pin_lag import meristem as guard
from tests.pin_lag.decide_double import answered_branch, serve_control_kinds
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401

_SCHEMA = {"component_id": "decide.schema.process.branch_select"}


def _schema_of(tenant: str, question_id: str) -> Mapping[str, Any] | None:
    return _SCHEMA if question_id == "process.branch_select" else None


def _recorder(seen: list[tuple[str, Any, str]]) -> Any:
    async def call(op: str, params: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
        seen.append((op, params, ref))
        return answered_branch("t2")

    return call


def _assert_control_params(request: Mapping[str, Any]) -> None:
    # One declared option ``decision``; exactly two text params (MEDULLA-DECISION-ENGINE-R180).
    assert [o["option_id"] for o in request["candidates"]["options"]] == ["decision"]
    assert [p["name"] for p in request["params"]] == ["graph", "process_decision"]
    graph, decision = request["params"]
    assert graph["value"] == {"type": "text", "value": "graph:one"}
    assert decision["value"]["type"] == "text"
    assert json.loads(decision["value"]["value"]) == {
        "plan_digest": "p1",
        "branch_task_id": "b1",
        "inputs": {
            "n": {"type": "integer", "value": 3},
            "ok": {"type": "boolean", "value": True},
        },
    }


@pytest.mark.spec("ARBORA-HOST-R001.2.5")
def test_a_branch_question_travels_as_one_wrapped_decide_request(
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_control_kinds(monkeypatch)
    seen: list[tuple[str, Any, str]] = []
    port = adapters.decision_port(
        _recorder(seen),
        tenant_of=lambda _: "tenant:one",
        schema_of=_schema_of,
        graph_of=lambda _: "graph:one",
    )
    payload = {
        "plan_digest": "p1",
        "branch_task_id": "b1",
        "inputs": {"n": 3, "ok": True},
    }
    verdict = asyncio.run(port("BranchSelect", payload, "t1"))
    assert verdict.taken_task_ids == ("t2",)
    [(op, params, ref)] = seen
    assert (op, ref) == ("eg.query.Decide", "t1")
    assert set(params) == {"request"}
    request = params["request"]
    assert request["tenant_id"] == "tenant:one"
    assert request["question"] == {
        "question_id": "process.branch_select",
        "kind": "branch_select",
        "safety": "ordinary",
    }
    assert request["feature_schema"] == _SCHEMA
    _assert_control_params(request)


@pytest.mark.spec("ARBORA-HOST-R001.2.5")
def test_a_missing_tenant_or_schema_sends_nothing(
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_control_kinds(monkeypatch)
    seen: list[tuple[str, Any, str]] = []
    for tenant, schema_of in (
        ("", _schema_of),
        ("tenant:one", None),
        ("tenant:one", lambda t, q: None),
    ):
        port = adapters.decision_port(
            _recorder(seen), tenant_of=lambda _, t=tenant: t, schema_of=schema_of
        )
        with pytest.raises(RuntimeError) as refused:
            asyncio.run(port("BranchSelect", {}, "t1"))
        assert refused.value.code == "decide_request_incomplete"  # type: ignore[attr-defined]
    assert seen == []


@pytest.mark.spec("ARBORA-HOST-R001.2.5")
def test_a_float_input_has_no_typed_form_and_sends_nothing(
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_control_kinds(monkeypatch)
    seen: list[tuple[str, Any, str]] = []
    engine = adapters.EngineDecisionAdapter(
        _recorder(seen),
        tenant_of=lambda _: "tenant:one",
        schema_of=_schema_of,
        graph_of=lambda _: "graph:one",
    )
    with pytest.raises(RuntimeError) as refused:
        asyncio.run(
            engine.decide("BranchSelect", {"inputs": {"ratio": 0.5}}, run_ref="run:1")
        )
    assert refused.value.code == "decide_param_untyped"  # type: ignore[attr-defined]
    assert seen == []


@pytest.mark.spec("ARBORA-HOST-R001.2.5")
def test_a_pin_that_lacks_the_question_kind_refuses_naming_it(
    main_shape: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from medulla.generated import models

    monkeypatch.setattr(models, "QuestionKind", Literal["route"] | Literal["rank"])
    seen: list[tuple[str, Any, str]] = []
    port = adapters.decision_port(
        _recorder(seen), tenant_of=lambda _: "tenant:one", schema_of=_schema_of
    )
    with pytest.raises(guard.PinLagUnavailable) as refused:
        asyncio.run(port("BranchSelect", {}, "t1"))
    assert refused.value.missing == "QuestionKind.branch_select"
    assert seen == []
