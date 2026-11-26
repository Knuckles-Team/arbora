"""Focused NE-086 policy/resolved-run acceptance fixtures."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Literal

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.control_plane.policy import (
    ApprovalDecision,
    CapabilityBinding,
    ExecutionBudget,
    PolicyAuthorization,
    PolicyRule,
    PolicyVersion,
)
from arbora.control_plane.runs import (
    ArtifactRef,
    AuditChain,
    EngineRunPolicy,
    InMemoryNativeAdmission,
    InMemoryObservationLedger,
    InvocationRef,
    JobRef,
    NativeAdmissionError,
    NativeAdmissionRequest,
    NativeWorkItemAdmission,
    ObservationDiscontinuityError,
    RelationalObservation,
    ReplayDriftError,
    RunResolution,
    TaskRef,
    ToolBindingRef,
    TraceRef,
    native_work_item_id,
)


def _digest(letter: str) -> str:
    """A distinct, deterministic, CONTRACT-VALID digest per label.

    ``Digest`` requires ``^sha256:[0-9a-f]{64}$``. The previous ``letter * 64``
    form produced a non-hex string for any non-hex label -- i, j, o, p, q, r, s
    and t are all used here -- which the model correctly rejected. Deriving the
    body from the label keeps every call site and its distinctness while
    actually satisfying the contract.
    """
    return "sha256:" + hashlib.sha256(letter.encode("utf-8")).hexdigest()


def _budget(**overrides: int) -> ExecutionBudget:
    values = {
        "max_tasks": 8,
        "max_depth": 8,
        "max_fanout": 4,
        "max_tool_calls": 4,
        "max_tokens": 10_000,
        "max_seconds": 120,
        "max_cost_micros": 10_000,
    }
    values.update(overrides)
    return ExecutionBudget(**values)


def _binding(letter: str = "a") -> CapabilityBinding:
    return CapabilityBinding(
        kind="tool",
        binding_id="tool:search",
        version="1.0.0",
        digest=_digest(letter),
        privilege="read",
    )


def _policy(*, effect: Literal["allow", "deny"] = "allow") -> PolicyVersion:
    return PolicyVersion(
        policy_id="policy:research",
        version="1.0.0",
        issuer_ref="authority:policy",
        budget=_budget(),
        rules=(
            PolicyRule(
                operation="run:research",
                effect=effect,
                approval_required=True,
                budget=_budget(),
            ),
        ),
        bindings=(_binding(),),
        max_privilege="read",
    )


class EngineDouble:
    """The engine's active policy: admits, or denies with one refusal code."""

    def __init__(self, deny: str | None = None) -> None:
        self.deny = deny
        self.asked: list[object] = []

    async def ask(self, authorization: object, now: int) -> None:
        self.asked.append(authorization)
        if self.deny is not None:
            raise EngineRefusal(self.deny)


def _engine_policy(engine: EngineDouble) -> EngineRunPolicy:
    return EngineRunPolicy(engine.ask)


def _approval(policy: PolicyVersion, *, request_digest: str) -> ApprovalDecision:
    return ApprovalDecision(
        approval_id="approval:research",
        policy_ref=policy.ref,
        operation="run:research",
        request_digest=request_digest,
        decision="approved",
        approver_refs=("principal:operator",),
        issued_at=90,
        expires_at=120,
        signer_ref="principal:operator",
        signature="sig:approval",
    )


def _resolution(*, request_digest: str) -> RunResolution:
    policy = _policy()
    approval = _approval(policy, request_digest=request_digest)
    # The authorization pins the exact approval reference the engine decided on.
    authorization = PolicyAuthorization(
        policy_ref=policy.ref,
        operation="run:research",
        request_digest=request_digest,
        bindings=(_binding(),),
        budget=_budget(),
        approval_ref=approval.ref,
        resolved_at=100,
    )
    job = JobRef(job_id="job:research", version="1.0.0", digest=_digest("j"))
    task = TaskRef(
        task_id="task:research",
        job_id=job.job_id,
        version="1.0.0",
        digest=_digest("t"),
    )
    tool = ToolBindingRef(binding=_binding())
    invocation = InvocationRef(
        invocation_id="invocation:research",
        task_id=task.task_id,
        tool_binding_id=tool.binding_id,
        tool_binding_digest=tool.digest,
        input_digest=request_digest,
        trace_ref=TraceRef(trace_id="trace:research", digest=_digest("r")),
        artifact_refs=(
            ArtifactRef(artifact_id="artifact:source", digest=_digest("s")),
        ),
    )
    return RunResolution(
        tenant_ref="tenant:demo",
        idempotency_key="request:001",
        request_digest=request_digest,
        authorization=authorization,
        job=job,
        tasks=(task,),
        tool_bindings=(tool,),
        invocations=(invocation,),
        trace_ref=invocation.trace_ref,
        artifact_refs=invocation.artifact_refs,
    )


def _admission_request(resolution: RunResolution) -> NativeAdmissionRequest:
    work_item_id = native_work_item_id(resolution.run_id)
    item = NativeWorkItemAdmission(
        run_id=resolution.run_id,
        work_item_id=work_item_id,
        tenant_ref=resolution.tenant_ref,
        idempotency_key=resolution.idempotency_key,
        kind="run:research",
        payload_ref="artifact:request",
        payload_digest=_digest("p"),
        request_digest=resolution.request_digest,
        resolution_digest=resolution.resolution_digest,
    )
    return NativeAdmissionRequest(resolution=resolution, work_item=item)


def test_policy_pins_exact_versions_and_rejects_budget_or_privilege_escalation() -> (
    None
):
    policy = _policy()
    assert policy.ref.digest == policy.policy_digest
    with pytest.raises(ValidationError, match="policy_rule_budget_escalation"):
        PolicyVersion(
            policy_id="policy:bad",
            version="1.0.0",
            issuer_ref="authority:policy",
            budget=_budget(max_tokens=10),
            rules=(
                PolicyRule(
                    operation="run:bad",
                    effect="allow",
                    budget=_budget(max_tokens=20),
                ),
            ),
        )
    with pytest.raises(ValidationError):
        CapabilityBinding(
            kind="tool",
            binding_id="tool:search",
            version="latest",
            digest=_digest("a"),
        )


def _request_and_admission(
    engine: EngineDouble,
) -> tuple[NativeAdmissionRequest, InMemoryNativeAdmission]:
    request_digest = _digest("q")
    resolution = _resolution(request_digest=request_digest)
    admission = InMemoryNativeAdmission(
        audit=AuditChain(max_events=8),
        clock=lambda: 100,
        policy=_engine_policy(engine),
    )
    return _admission_request(resolution), admission


@pytest.mark.spec("ARBORA-OPS-R050.3")
async def test_admission_is_one_run_one_work_item_and_duplicate_delivery_is_idempotent() -> (
    None
):
    engine = EngineDouble()
    request, admission = _request_and_admission(engine)
    resolution = request.resolution

    first = await admission.admit_once(request)
    second = await admission.admit_once(request)
    assert first.created is True
    assert second.created is False
    record = admission.read(resolution.run_id)
    assert record is not None
    assert record.resolution == resolution
    # The engine decided once; the replay never asks again.
    assert len(engine.asked) == 1

    # Drift the request digest COHERENTLY: RunResolution's own validator
    # requires authorization.request_digest == request_digest, so update both so
    # the object is valid but genuinely differs from the stored one.
    drifted_digest = _digest("d")
    altered = resolution.model_copy(
        update={
            "request_digest": drifted_digest,
            "authorization": resolution.authorization.model_copy(
                update={"request_digest": drifted_digest}
            ),
        }
    )
    altered_item = request.work_item.model_copy(
        update={
            "request_digest": altered.request_digest,
            "resolution_digest": altered.resolution_digest,
        }
    )
    with pytest.raises(ReplayDriftError, match="replay_or_body_drift"):
        await admission.admit_once(
            NativeAdmissionRequest(resolution=altered, work_item=altered_item)
        )


@pytest.mark.spec("ARBORA-OPS-R050.3")
async def test_concurrent_duplicate_delivery_admits_once() -> None:
    engine = EngineDouble()
    request, admission = _request_and_admission(engine)
    receipts = await asyncio.gather(*map(admission.admit_once, [request] * 4))
    assert [receipt.created for receipt in receipts].count(True) == 1
    assert len(engine.asked) == 1


@pytest.mark.spec("ARBORA-OPS-R050.3")
@pytest.mark.parametrize(
    "code",
    [
        "SCOPE_EXCEEDS_POLICY",
        "POLICY_REVIEW_MISSING",
        "POLICY_REVIEW_MISMATCH",
        "REVIEW_NOT_INDEPENDENT",
    ],
)
async def test_admission_refuses_when_the_engine_denies(code: str) -> None:
    engine = EngineDouble(deny=code)
    request, admission = _request_and_admission(engine)
    with pytest.raises(NativeAdmissionError, match="policy_denied") as refused:
        await admission.admit_once(request)
    assert code in str(refused.value)
    # A denied run records nothing: no run, no work item, no audit event.
    assert admission.read(request.resolution.run_id) is None
    assert admission.audit is not None and len(admission.audit._events) == 0


@pytest.mark.spec("ARBORA-OPS-R050.3")
async def test_admission_fails_closed_when_the_engine_is_unreachable() -> None:
    class Down(EngineDouble):
        async def ask(self, authorization: object, now: int) -> None:
            raise TimeoutError

    request, admission = _request_and_admission(Down())
    with pytest.raises(NativeAdmissionError, match="policy_unavailable"):
        await admission.admit_once(request)
    assert admission.read(request.resolution.run_id) is None


@pytest.mark.spec("ARBORA-OPS-R050.3")
def test_arbora_keeps_no_local_policy_authority() -> None:
    import importlib

    import arbora.control_plane.policy as policy_package

    assert not hasattr(policy_package, "PolicyAuthority")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("arbora.control_plane.policy.repository")


def test_audit_chain_and_observations_fail_on_discontinuity_or_regression() -> None:
    chain = AuditChain(max_events=2)
    chain.append(
        kind="run.admitted",
        subject_ref="run:one",
        payload_digest=_digest("a"),
        observed_at=1,
    )
    chain.append(
        kind="work_item.admitted",
        subject_ref="work_item:one",
        payload_digest=_digest("b"),
        observed_at=1,
    )
    assert chain.verify()
    with pytest.raises(ValueError, match="audit_capacity_exceeded"):
        chain.append(
            kind="run.duplicate",
            subject_ref="run:one",
            payload_digest=_digest("c"),
            observed_at=2,
        )

    ledger = InMemoryObservationLedger()
    row = RelationalObservation(
        observation_id="observation:one",
        run_id="run:one",
        work_item_id="work_item:one",
        sequence=2,
        observed_state="pending",
        source_ref="mirror:relational",
        evidence_digest=_digest("e"),
        observed_at=1,
    )
    assert ledger.append(row) is True
    assert ledger.append(row) is False
    # A regressed sequence must arrive as a DISTINCT observation. Reusing
    # observation:one with different content trips the identity-drift check
    # first (it is evaluated before the sequence check, and is the stronger
    # violation), so the sequence path would never be exercised -- the
    # run-identity case immediately below already follows this convention.
    with pytest.raises(ObservationDiscontinuityError, match="sequence_regressed"):
        ledger.append(
            row.model_copy(
                update={"observation_id": "observation:regressed", "sequence": 1}
            )
        )
    with pytest.raises(ObservationDiscontinuityError, match="run_identity_drift"):
        ledger.append(
            row.model_copy(
                update={
                    "observation_id": "observation:other-run",
                    "run_id": "run:other",
                    "sequence": 3,
                    "observed_at": 2,
                }
            )
        )
    with pytest.raises(ObservationDiscontinuityError, match="time_regressed"):
        ledger.append(
            row.model_copy(
                update={
                    "observation_id": "observation:old-time",
                    "sequence": 3,
                    "observed_at": 0,
                }
            )
        )


def test_definition_side_objects_do_not_claim_or_complete_work() -> None:
    policy = _policy()
    approval = _approval(policy, request_digest=_digest("q"))
    admission = InMemoryNativeAdmission()
    assert not hasattr(admission, "claim_work_item")
    assert not hasattr(admission, "renew_work_item_lease")
    assert not hasattr(admission, "commit_work_item_result")
    assert not hasattr(approval, "result")
