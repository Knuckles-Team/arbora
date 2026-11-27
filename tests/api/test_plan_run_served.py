"""``agents.plan_run`` served over HTTP and MCP against the loopback stack.

Rows ARBORA-RELEASE-R003.4.10 and R003.4.17 name the hot path: the call enters
the real invoke pipeline of the served app (HTTP) and of the served MCP verbs,
reaches ``handle_plan_run`` and runs its tool steps. What is real here: the
served app and MCP surface, authentication, the invoke pipeline with its effect
journal and audit, ``handle_plan_run``, the task-plan run loop and its
bindings. What is recorded: the engine's TaskPlan decision (a recorded engine
speaking the TaskPlan contract), the prompt model, and the bound step pipeline
that answers each governed ``fleet.call`` as the loopback harness records it
(one effect per idempotency key).
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import itertools
from typing import Any

import pytest

pytest.importorskip(
    "meristem.graph",
    reason="meristem.graph ships from meristem 6.0.0; the pinned meristem lacks it",
)

from arbora.api.invoke.steps import OpResult  # noqa: E402
from tests.api._loopback_engine import PRINCIPAL, Loopback, loopback  # noqa: E402,F401
from tests.api._served_support import McpAsk, person  # noqa: E402
from tests.api.test_plan_run_ops import (  # noqa: E402
    BINDING_PLAN,
    FOREACH_PLAN,
    TOOL_PIN,
    ToolEngine,
    WriteGateway,
    _call_task,
    _json_model,
    _plan,
)
from tests.test_served_operation_api import served  # noqa: E402,F401

OP = "agents.plan_run"
_RUNS = itertools.count()


class ServedToolEngine(ToolEngine):
    """The recorded TaskPlan engine, answering for the served caller."""

    async def decide(self, request: dict[str, Any]) -> dict[str, Any]:
        answered = await super().decide(request)
        principal = "principal:sha256:" + hashlib.sha256(PRINCIPAL.encode()).hexdigest()
        for record in answered["records"]:
            record["caller_principal"] = principal
        return answered

    @classmethod
    def verify(cls, record, request, *, expected_principal):
        return super().verify(record, request, expected_principal=PRINCIPAL)


def loopback_bindings() -> dict[str, Any]:
    return {
        "task_plan_engine": ServedToolEngine(),
        "task_plan_model": _json_model({"emit": '{"title": "T1"}'}),
        "task_plan_feature_schema": TOOL_PIN,
        "task_plan_work_engine": None,
        "fleet_gateway": WriteGateway(),
    }


def _echo(params: dict[str, Any]) -> OpResult:
    value = {"echo": dict(params["arguments"]), "token": "sk-" + "x" * 24}
    return OpResult(value={"value": value})


def _ids(ids: list[str]) -> Any:
    return lambda params: OpResult(value={"value": {"ids": [{"id": i} for i in ids]}})


def _prepare(loop: Loopback, **answers: Any) -> None:
    loop.steps.journal_effects = True
    loop.steps.answers.update({"create": _echo, "attach": _echo, "write": _echo})
    loop.steps.answers.update(answers)
    loop.serve({})


def _mcp(loop: Loopback, monkeypatch: pytest.MonkeyPatch) -> McpAsk:
    return McpAsk(
        loop.invocation, monkeypatch, person("one", extra_scopes=("fleet:control",))
    )


def _mcp_run(ask: McpAsk, plan: dict[str, Any], key: str) -> Any:
    return ask("act", OP, {"plan": plan}, idempotency_key=key, execute=True)


def _run_value(payload: Any) -> dict[str, Any]:
    result = payload.get("result", payload) if isinstance(payload, dict) else payload
    value = result.get("value", result) if isinstance(result, dict) else result
    assert isinstance(value, dict) and "run" in value, payload
    return value


def _step(value: dict[str, Any], task: str) -> dict[str, Any]:
    (step,) = [s for s in value["run"]["steps"] if s["task_id"] == task]
    return step


@pytest.mark.spec("ARBORA-RELEASE-R003.4.10")
def test_cross_connector_plan_served_over_http_and_mcp(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(loopback)
    reply = loopback.call(OP, {"plan": BINDING_PLAN}, key="plan-http-1")
    value = Loopback.value(reply)
    assert value["run"]["status"] == "completed"
    assert value["plan_digest"].startswith("sha256:")
    effects = loopback.steps.effects
    assert [tool for tool, _, _ in effects] == ["create", "attach"]
    assert effects[0][1] == {"title": "T1", "n": 2}
    assert effects[1][1]["ref"] == "T1"
    keys = [key for _, _, key in effects]
    assert len(set(keys)) == 2 and all(k.startswith("plan-step:run-") for k in keys)
    assert _step(value, "open")["idempotency_key"] == keys[0]
    assert "hunter2" not in reply.text and "sk-" + "x" * 24 not in reply.text
    # The same served call again repeats no effect.
    again = loopback.call(OP, {"plan": BINDING_PLAN}, key="plan-http-1")
    assert again.status_code == 200 and len(loopback.steps.effects) == 2
    # The MCP door reaches the same handler and the same steps.
    ask = _mcp(loopback, monkeypatch)
    over_mcp = _run_value(_mcp_run(ask, BINDING_PLAN, "plan-mcp-1"))
    assert over_mcp["run"]["status"] == "completed"
    assert over_mcp["plan_digest"] == value["plan_digest"]
    assert [t for t, _, _ in loopback.steps.effects] == ["create", "attach"] * 2


@pytest.mark.spec("ARBORA-RELEASE-R003.4.10")
def test_binding_to_a_missing_path_fails_the_step_and_starts_nothing_served(
    loopback: Loopback,
) -> None:
    _prepare(loopback)
    missing = _plan(
        {"task_id": "emit", "prompt": "emit:: json", "depends_on": []},
        _call_task(
            "open", "a", "create", {"x": {"$ref": "steps.emit.output.nope"}}, "emit"
        ),
        _call_task("link", "b", "attach", {}, "open"),
        output="link",
    )
    value = Loopback.value(loopback.call(OP, {"plan": missing}, key="plan-missing-1"))
    assert loopback.steps.effects == []
    assert _step(value, "open")["outcome"] == "failed"
    assert _step(value, "open")["failed_reference"]
    assert _step(value, "link")["outcome"] == "refused"
    assert value["run"]["status"] != "completed"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.17")
def test_foreach_plan_served_over_http_and_mcp_writes_each_item_once(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(loopback, list_ids=_ids(["x", "y", "z"]))
    value = Loopback.value(loopback.call(OP, {"plan": FOREACH_PLAN}, key="each-http-1"))
    assert value["run"]["status"] == "completed"
    writes = [e for e in loopback.steps.effects if e[0] == "write"]
    assert [w[1] for w in writes] == [
        {"id": "x", "at": 0},
        {"id": "y", "at": 1},
        {"id": "z", "at": 2},
    ]
    assert [i["index"] for i in _step(value, "each")["items"]] == [0, 1, 2]
    ask = _mcp(loopback, monkeypatch)
    over_mcp = _run_value(_mcp_run(ask, FOREACH_PLAN, "each-mcp-1"))
    assert over_mcp["run"]["status"] == "completed"
    assert len([e for e in loopback.steps.effects if e[0] == "write"]) == 6


@pytest.mark.spec("ARBORA-RELEASE-R003.4.17")
def test_foreach_duplicate_id_fails_at_the_second_equal_item_served(
    loopback: Loopback, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(loopback, list_ids=_ids(["x", "y", "x"]))
    each = FOREACH_PLAN["tasks"][1]
    plan = _plan(
        FOREACH_PLAN["tasks"][0],
        {**each, "call": {**each["call"], "arguments": {"id": {"$ref": "item.id"}}}},
        output="each",
    )
    http = Loopback.value(loopback.call(OP, {"plan": plan}, key="dup-http-1"))
    assert http["run"]["status"] != "completed"
    assert len([e for e in loopback.steps.effects if e[0] == "write"]) == 2
    ask = _mcp(loopback, monkeypatch)
    over_mcp = _run_value(_mcp_run(ask, plan, "dup-mcp-1"))
    assert over_mcp["run"]["status"] != "completed"
    assert len([e for e in loopback.steps.effects if e[0] == "write"]) == 4
