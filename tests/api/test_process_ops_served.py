"""A process runs over the served operations, waits at an approval and follows its branch (ARBORA-OPS-R898.8).

The engine is a recording loopback: ``Decide`` seals the plan and each control-step verdict,
and the key-value journal holds the approval waits. The pinned meristem predates the control
step kinds and ``HostPorts``, so the run loop is the structural double of
``tests/processes/_support.py``; everything between the HTTP route and that loop is real:
the invoke pipeline, ``process.plan``, prompt resolution, the port binding, the approval
broker and the run journal. The approver's decision lands in the journal between the wait's
write and its next read (the decision operation itself is proven by ARBORA-OPS-R898.4).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from tests.api._served_support import confirmable_invocation, person, route_methods
from tests.api.test_process_ops import _structural_request
from tests.pin_lag.test_host_ports_pin_lag import main_shape  # noqa: F401
from tests.processes._support import (
    PLAN,
    PROMPTS,
    decide_record,
    main_run_task,  # noqa: F401
    run_loop,
)
from tests.question_kinds import declare_question_kinds
from tests.test_served_operation_api import _call, _value, served  # noqa: F401, F811

KEY = "Idempotency-Key"
SCOPES = ("process:read", "process:run", "process:approve")


class LoopbackEngine:
    """``Decide``, node reads and the key-value journal of the recording engine."""

    def __init__(
        self, verdict: str, arm: str = "x1", tenant: str = "tenant:one"
    ) -> None:
        self.verdict, self.arm, self.tenant = verdict, arm, tenant
        self.rows: dict[str, tuple[int, str]] = {}
        self.decides: list[str] = []

    def decide(self, params: dict[str, Any], who: str) -> Any:
        request = params["request"]
        kind = request["question"]["kind"]
        self.decides.append(kind)
        if request["tenant_id"] != self.tenant:
            from medulla import EngineResponseError

            raise EngineResponseError("WORKFLOW_UNKNOWN", "no such workflow")
        value = PLAN if kind == "process_plan" else {"taken_task_ids": [self.arm]}
        record = decide_record(request, value)
        import hashlib

        record["caller_principal"] = (
            "principal:sha256:" + hashlib.sha256(who.encode()).hexdigest()
        )
        return {"records": [record]}

    def node(self, params: dict[str, Any], _who: str) -> Any:
        text = PROMPTS.get(params["node_id"])
        return {"properties": {"content": text}} if text else {}

    def get(self, params: dict[str, Any], _who: str) -> Any:
        version, value = self.rows.get(params["key"], (0, ""))
        return {"value": value, "version": version} if version else None

    def cas(self, params: dict[str, Any], _who: str) -> Any:
        key, new = params["key"], json.loads(params["new"])
        version = self.rows.get(key, (0, ""))[0]
        assert version == params["expected_version"]
        if new.get("kind") == "approval" and new["state"] == "pending":
            # The approver decides while the run waits.
            new = {
                **new,
                "state": "decided",
                "verdict": self.verdict,
                "approver": "person:finance",
                "decided_ms": 9,
            }
        self.rows[key] = (version + 1, json.dumps(new))
        return {"version": version + 1}

    def scan(self, params: dict[str, Any], _who: str) -> Any:
        rows = [
            {"value": v, "version": n}
            for k, (n, v) in sorted(self.rows.items())
            if k.startswith(params["prefix"])
        ]
        return {"entries": rows}


def _env(
    harness: Any, monkeypatch: pytest.MonkeyPatch, engine: LoopbackEngine, tenant: str
) -> Any:
    import medulla.decision_client as decision_client

    from arbora.api.ops import plans, processes

    app, client = harness
    declare_question_kinds(
        monkeypatch, "process_plan", "branch_select", "decision_table_eval"
    )
    monkeypatch.setattr(
        decision_client, "process_plan_request", _structural_request, raising=False
    )

    async def pin(_context: Any, _tenant: str) -> dict[str, Any]:
        return {"component_id": "pin", "digest": "d"}

    monkeypatch.setattr(plans, "_feature_schema", pin)
    monkeypatch.setattr(plans, "_plan_model", lambda _c: "model:one")
    monkeypatch.setattr(plans, "execute_task_plan", run_loop)
    monkeypatch.setattr(
        processes,
        "_published_schema",
        lambda _c: lambda _t, q: {"component_id": f"decide.schema.{q}"},
    )
    route_methods(
        client,
        {
            "Decide": engine.decide,
            "GetNodeProperties": engine.node,
            "KvGet": engine.get,
            "KvCas": engine.cas,
            "KvScan": engine.scan,
        },
        monkeypatch,
        {"fixture-verified": person("one", tenant=tenant, extra_scopes=SCOPES)},
    )
    return app


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    from arbora.api.ops import registry_factory

    # The process scopes are not in the pinned engine catalog yet; serve them as listed.
    monkeypatch.setattr(
        registry_factory,
        "apply_pending_scopes",
        lambda curated, _s: (list(curated), []),
    )
    from arbora.security import request_identity

    known = request_identity.session_scopes
    monkeypatch.setattr(
        request_identity, "session_scopes", lambda: frozenset(known()) | set(SCOPES)
    )
    return confirmable_invocation(monkeypatch)


@pytest.mark.spec("ARBORA-OPS-R898.8")
def test_process_run_waits_for_approval_and_follows_branch(
    stack: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    main_run_task: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = LoopbackEngine("approve", arm="x2")
    app = _env(served, monkeypatch, engine, "tenant:one")
    params = {"workflow_id": "wf", "version": 2}
    plan = _value(_call(app, "process.plan", params))
    assert plan["plan_digest"] == PLAN["plan_digest"]
    run = _value(_call(app, "process.run", params, **{KEY: "run-1"}))
    # The run waited at the approval, resumed on the approve and took the engine's arm.
    assert run["status"] == "completed"
    assert run["tasks"]["ap"] == {"verdict": "approve"}
    assert run["tasks"]["b1"] == {"taken": ["x2"]}
    assert "x1" not in run["tasks"] and "x2" in run["tasks"]
    assert engine.decides.count("branch_select") == 1
    approvals = _value(_call(app, "process.approvals.list", {"run_id": run["run_id"]}))
    assert [a["approver"] for a in approvals["items"]] == ["person:finance"]
    # A replay of the key starts no second run.
    again = _value(_call(app, "process.run", params, **{KEY: "run-1"}))
    assert again["run_id"] == run["run_id"]
    assert engine.decides.count("branch_select") == 1


@pytest.mark.spec("ARBORA-OPS-R898.8")
def test_a_reject_ends_the_run_and_skips_the_dependents(
    stack: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    main_run_task: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = LoopbackEngine("reject")
    app = _env(served, monkeypatch, engine, "tenant:one")
    run = _value(
        _call(app, "process.run", {"workflow_id": "wf", "version": 2}, **{KEY: "r2"})
    )
    assert run["status"] == "failed"
    assert run["tasks"]["ap"] == {"verdict": "reject"}
    assert "b1" not in run["tasks"] and "x1" not in run["tasks"]
    assert "branch_select" not in engine.decides


@pytest.mark.spec("ARBORA-OPS-R898.8")
def test_a_second_tenant_gets_workflow_unknown(
    stack: Any,
    served: Any,  # noqa: F811
    main_shape: None,  # noqa: F811
    main_run_task: None,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = LoopbackEngine("approve", tenant="tenant:two")  # the workflow is theirs
    app = _env(served, monkeypatch, engine, "tenant:one")
    refused = _call(app, "process.plan", {"workflow_id": "wf", "version": 2})
    assert refused.status_code == 404
    assert refused.json()["error"]["code"] == "WORKFLOW_UNKNOWN"
