"""Host port bindings: refusal under the pinned release, real path against main's shape.

The refusal tests run under the current meristem pin. The real-path tests install a
structural double of meristem ``main``'s ``host_ports`` module and run the adapters
through it.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from typing import Any

import pytest

from arbora.agent_host import host_port_adapters as adapters
from arbora.pin_lag import meristem as guard


def _pinned_has_host_ports() -> bool:
    from meristem import host_ports

    return hasattr(host_ports, "HostPorts")


@dataclasses.dataclass(frozen=True)
class _Answer:
    outcome: str
    option_ids: tuple[str, ...] = ()
    refusal_code: str | None = None
    record_digest: str | None = None
    payload: Mapping[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _Verdict:
    record_id: str = ""
    taken_task_ids: tuple[str, ...] = ()
    outputs: Any = None
    abstention: str | None = None


@dataclasses.dataclass(frozen=True)
class _Approval:
    verdict: str
    approver: str = ""
    decided_at_ms: int = 0


@dataclasses.dataclass(frozen=True)
class _Confirm:
    verdict: str
    ref: str | None = None
    refusal_code: str | None = None


@dataclasses.dataclass(frozen=True)
class _Summary:
    output: str
    provider: str
    model: str
    policy_revision: str


class _Refused(RuntimeError):
    def __init__(self, code: str, *, method: str | None = None) -> None:
        self.code, self.method = code, method
        super().__init__(code)


class _Disabled(RuntimeError):
    pass


@dataclasses.dataclass
class _HostPorts:
    engine_decision: Any = None
    decision: Any = None
    approval: Any = None
    confirm: Any = None
    summary_provider: Any = None
    study_content: Any = None
    run_trace: Any = None
    technique_measure: Any = None
    sandbox_processes: Any = None
    liveness_wait: Any = None
    checkpoint_store: Any = None
    changeset_submitter: Any = None
    gate_runner: Any = None


@pytest.fixture
def main_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    from meristem import host_ports

    for name, value in {
        "HostPorts": _HostPorts,
        "EngineAnswer": _Answer,
        "EngineDecisionRefused": _Refused,
        "DecisionVerdict": _Verdict,
        "ApprovalOutcome": _Approval,
        "ConfirmVerdict": _Confirm,
        "SummaryResult": _Summary,
        "SummaryProviderDisabled": _Disabled,
    }.items():
        monkeypatch.setattr(host_ports, name, value, raising=False)


@pytest.mark.spec("ARBORA-OPS-R890")
@pytest.mark.spec("ARBORA-OPS-R894")
@pytest.mark.skipif(_pinned_has_host_ports(), reason="the pin carries HostPorts")
def test_pinned_release_refuses_every_host_port_binding_naming_the_gap() -> None:
    with pytest.raises(guard.PinLagUnavailable) as record:
        guard.host_ports_type()
    assert record.value.code == "UNAVAILABLE"
    assert record.value.missing == "meristem.host_ports.HostPorts"
    with pytest.raises(guard.PinLagUnavailable) as types:
        guard.port_types()
    assert types.value.missing == "meristem.host_ports.EngineAnswer"
    binding = adapters.bind_host_ports(
        engine_decision=object(), checkpoint_store=object()
    )
    assert binding.ports is None and binding.refusal is not None
    assert binding.refusal.missing == "meristem.host_ports.HostPorts"

    async def call(
        op: str, params: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        return {"outcome": "advisory"}

    with pytest.raises(guard.PinLagUnavailable):
        asyncio.run(
            adapters.EngineDecisionAdapter(call).log("Evaluate", {}, run_ref="r")
        )
    with pytest.raises(ValueError):
        guard.build_host_ports(nonexistent=1)


def test_a_missing_field_names_that_field(monkeypatch: pytest.MonkeyPatch) -> None:
    from meristem import host_ports

    @dataclasses.dataclass
    class Old:
        decision: Any = None

    monkeypatch.setattr(host_ports, "HostPorts", Old, raising=False)
    with pytest.raises(guard.PinLagUnavailable) as record:
        guard.build_host_ports(decision=object(), approval=object())
    assert record.value.missing == "meristem.host_ports.HostPorts.approval"


def test_media_symbols_refuse_when_the_pinned_engine_lacks_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(generated, "METHOD_IDS", frozenset(), raising=False)
    with pytest.raises(guard.PinLagUnavailable) as record:
        guard.media_methods()
    assert record.value.missing == "medulla.generated.METHOD_IDS.MediaPut"
    with pytest.raises(guard.PinLagUnavailable) as model_input:
        guard.assembly_model_input(object())
    assert model_input.value.missing == "medulla AssemblyResult.model_input"


@pytest.mark.pin_lag
def test_real_path_binds_every_port_through_the_main_shape(
    main_shape: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.pin_lag.decide_double import answered_branch, serve_control_kinds

    serve_control_kinds(monkeypatch)
    seen: list[tuple[str, Mapping[str, Any], str]] = []

    async def call(
        op: str, params: Mapping[str, Any], run_ref: str
    ) -> Mapping[str, Any]:
        seen.append((op, params, run_ref))
        return {"outcome": "committed", "record_digest": "d1", "payload": {"k": 1}}

    async def scenario() -> None:
        engine = adapters.EngineDecisionAdapter(
            call, lambda m: "eg.storage.MemoryRecall" if m == "MemoryRecall" else None
        )
        answer = await engine.log("Evaluate", {"round": 2}, run_ref="run:1")
        assert (answer.outcome, answer.record_digest) == ("committed", "d1")
        assert seen[-1] == (
            "eg.coordination.DecisionLog",
            {"op": {"op": "evaluate", "round": 2}},
            "run:1",
        )
        await engine.read("MemoryRecall", {"q": 1}, run_ref="run:1")
        with pytest.raises(_Refused) as forbidden:
            await engine.write("Nope", {}, run_ref="run:1")
        assert forbidden.value.code == "engine_method_forbidden"

        async def verdict(
            op: str, params: Mapping[str, Any], ref: str
        ) -> Mapping[str, Any]:
            return answered_branch("t2")

        taken = await adapters.decision_port(
            verdict,
            tenant_of=lambda _: "tenant:one",
            schema_of=lambda t, q: {"component_id": f"decide.schema.{q}"},
            graph_of=lambda _: "graph:one",
        )("BranchSelect", {}, "t1")
        assert taken.taken_task_ids == ("t2",)

        gate = asyncio.Event()
        decided = asyncio.Event()

        async def waiting(
            run: str, task: str, role: str, plan: str
        ) -> Mapping[str, Any]:
            await decided.wait()
            return {"verdict": "approve", "approver": "ann", "decided_at_ms": 5}

        async def clock(seconds: float) -> None:
            await gate.wait()

        waiter = adapters.approval_waiter(waiting, clock)
        decided.set()
        assert (
            await waiter("r", "t", "owner", 30, "arbora_plan:p")
        ).verdict == "approve"
        decided.clear()
        gate.set()
        assert (
            await waiter("r", "t", "owner", 30, "arbora_plan:p")
        ).verdict == "timeout"

        async def broken(
            kind: str, payload: Mapping[str, Any], ref: str
        ) -> Mapping[str, Any]:
            raise RuntimeError("down")

        denied = await adapters.plan_confirmer(broken)("tool_action", {}, "r")
        assert (denied.verdict, denied.refusal_code) == (
            "deny",
            "confirm_unavailable",
        )

        async def off(context: Mapping[str, Any], ref: str) -> None:
            return None

        with pytest.raises(_Disabled):
            await adapters.summary_provider(off)({}, "a")

        async def missing(t: str, g: str, n: str, r: int) -> None:
            return None

        with pytest.raises(LookupError):
            await adapters.study_content_reader(missing)("t", "g", "n", 1)

        async def bad(technique: str, arm: str, digest: str) -> Mapping[str, Any]:
            return {"score": "high"}

        with pytest.raises(ValueError):
            await adapters.technique_measure(bad)("t", "a", "d")

    asyncio.run(scenario())
    rows = adapters.bind_host_ports(engine_decision=object(), gate_runner=object())
    assert rows.refusal is None and rows.ports is not None
    assert rows.ports.engine_decision is not None
    assert adapters.gate_runner_factory(object(), lambda: False)("run") is None
