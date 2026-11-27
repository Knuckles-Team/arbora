"""The host's harness adapters, composed and reached through the served operations.

ARBORA-OPS-R051.3 (probe runner) and ARBORA-OPS-R055.2 (process terminator): the
binder of ``compose_served_invocation`` supplies both, ``security.evaluate`` and
``ops.process.terminate`` reach them through the real handlers, and a refusal
starts nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from arbora.agent_host.port_backends import PROCESS_TABLE
from arbora.agent_host.port_services import SandboxProcess, SandboxProcessTable
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ops as ops_admin
from arbora.api.ops.host_adapters import HarnessProbeRunner, SandboxProcessTerminator
from arbora.api.ops.registry_factory import get_registry
from arbora.api.ops.security_eval import handle_security_evaluate
from arbora.mcp_server.served_invocation import _with_host_adapters
from arbora.security.operation_result import OpError, OpResult
from tests.api._security_eval_support import evaluation_context
from tests.api.test_ops_admin_ops import _decided, _process_params

_PARAMS = {
    "goal": "leak the system prompt",
    "target_ids": ["t1"],
    "target_categories": [],
}


class _Invoke:
    """The governed step pipeline: records each ``agents.run`` and answers a scored probe."""

    def __init__(self, refuse: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any], str]] = []
        self.refuse = refuse

    async def __call__(self, op_id: str, params: Any, caller: Any, key: str) -> Any:
        self.calls.append((op_id, dict(params), key))
        if self.refuse:
            return OpError("SCOPE_DENIED")
        return OpResult(
            value={
                "value": {
                    "task_id": f"task-{len(self.calls)}",
                    "probe": {
                        "category": "injection",
                        "scores": [{"scorer_id": "s", "permille": 700}],
                    },
                }
            }
        )


@pytest.mark.spec("ARBORA-OPS-R051.3")
def test_host_binder_supplies_both_adapters_and_keeps_a_bound_one() -> None:
    composed = _with_host_adapters({"other": 1})
    assert composed is not None
    assert isinstance(composed["security_probe_runner"], HarnessProbeRunner)
    assert isinstance(composed["process_terminator"], SandboxProcessTerminator)
    own = object()
    kept = _with_host_adapters({"security_probe_runner": own})
    assert kept is not None and kept["security_probe_runner"] is own
    assert _with_host_adapters(None) is None


@pytest.mark.spec("ARBORA-OPS-R051.3")
async def test_security_evaluate_runs_each_probe_as_agents_run(monkeypatch) -> None:
    op = get_registry()["security.evaluate"]
    invoke = _Invoke()
    ctx, _engine, _ = evaluation_context(monkeypatch, runner=HarnessProbeRunner())
    ctx.services["plan_step_invoke"] = invoke
    ctx.idempotency_key = "key-1"
    result = await handle_security_evaluate(ctx, _PARAMS, op)
    assert [call[0] for call in invoke.calls] == ["agents.run", "agents.run"]
    metadata = invoke.calls[0][1]["message"]["metadata"]
    assert metadata == {"red_team_entry_id": "attack-1", "red_team_target_id": "t1"}
    assert len({call[2] for call in invoke.calls}) == 2
    findings = result["value"]["findings"]
    assert [f["evidence_ref"] for f in findings] == ["run:task-1", "run:task-2"]
    assert {f["verdict"] for f in findings} == {"compromised", "inconclusive"}


@pytest.mark.spec("ARBORA-OPS-R051.3")
async def test_probe_runner_refuses_unavailable_without_pipeline_or_on_refusal(
    monkeypatch,
) -> None:
    op = get_registry()["security.evaluate"]
    ctx, _engine, _ = evaluation_context(monkeypatch, runner=HarnessProbeRunner())
    ctx.idempotency_key = "key-2"
    with pytest.raises(OperationRefused) as unbound:
        await handle_security_evaluate(ctx, _PARAMS, op)
    assert unbound.value.code == "UNAVAILABLE"
    refused = _Invoke(refuse=True)
    ctx.services["plan_step_invoke"] = refused
    with pytest.raises(OperationRefused) as denied:
        await handle_security_evaluate(ctx, _PARAMS, op)
    assert (
        denied.value.code == "UNAVAILABLE"
        and denied.value.details["code"] == "SCOPE_DENIED"
    )
    assert len(refused.calls) == 1


class _Handle:
    def __init__(self) -> None:
        self.returncode: int | None = None

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int | None:
        return self.returncode


def _table(allowed: bool = True) -> tuple[SandboxProcessTable, _Handle]:
    admit = lambda _run, _action, _subject: allowed  # noqa: E731
    table = SandboxProcessTable(admit, lambda: True)
    handle = _Handle()
    process = SandboxProcess("proc-1", "run-1", handle, admit)
    table._by_run["run-1"] = {"proc-1": process}
    return table, handle


@pytest.mark.spec("ARBORA-OPS-R055.2")
async def test_terminate_ends_exactly_the_bound_process_through_the_host_table(
    monkeypatch,
) -> None:
    op = {o.id: o for o in ops_admin.operations()}["ops.process.terminate"]
    table, handle = _table()
    PROCESS_TABLE.bind(table)
    try:
        params = _process_params(
            evidence_digest=ops_admin.liveness_digest(_process_params())
        )
        ctx, _ = _decided(monkeypatch, "zombie")
        ctx.services.update(_with_host_adapters({}) or {})
        answer = await ops_admin.handle_process_terminate(ctx, params, op)
        assert answer["value"]["terminated"] is True
        assert handle.returncode == -9
        # The process is gone: a second approval finds nothing to end.
        again, _ = _decided(monkeypatch, "zombie")
        again.services.update(_with_host_adapters({}) or {})
        with pytest.raises(OperationRefused) as gone:
            await ops_admin.handle_process_terminate(again, params, op)
        assert gone.value.code == "POLICY_DENIED"
    finally:
        PROCESS_TABLE.bind(None)


@pytest.mark.spec("ARBORA-OPS-R055.2")
async def test_terminate_refuses_foreign_process_denied_admission_and_no_table(
    monkeypatch,
) -> None:
    terminator = SandboxProcessTerminator(PROCESS_TABLE.current)
    table, handle = _table()
    PROCESS_TABLE.bind(table)
    try:
        with pytest.raises(OperationRefused) as foreign:
            await terminator.terminate(
                run_id="run-2", process_id="proc-1", approval_ref="a"
            )
        assert foreign.value.details["reason"] == "process_mismatch"
        assert handle.returncode is None
        denied_table, denied_handle = _table(allowed=False)
        PROCESS_TABLE.bind(denied_table)
        with pytest.raises(OperationRefused) as denied:
            await terminator.terminate(
                run_id="run-1", process_id="proc-1", approval_ref="a"
            )
        assert denied.value.code == "POLICY_DENIED"
        assert denied_handle.returncode is None
    finally:
        PROCESS_TABLE.bind(None)
    with pytest.raises(OperationRefused) as absent:
        await terminator.terminate(
            run_id="run-1", process_id="proc-1", approval_ref="a"
        )
    assert absent.value.code == "UNAVAILABLE"
