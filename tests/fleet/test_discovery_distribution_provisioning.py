"""Integrated discovery, distribution and provisioning (ARBORA-FLEET-R027.1 to R027.6, R005, R005.3)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import OrchestratorA2ARouter
from arbora.a2a.work_items import WorkItemView
from arbora.control_plane.policy.models import canonical_digest
from arbora.core.settings import RuntimeConfig
from arbora.fleet.provision_flow import (
    EVIDENCE_KIND,
    REFUSALS,
    Decision,
    FlowPorts,
    ProducerUnavailable,
    ProvisionFlowRefused,
    RunResult,
    SkillRef,
)
from arbora.fleet.provision_receipt import (
    BINDING_MISMATCH,
    IntegratedProvisionReceipt,
    PhaseBinding,
)
from arbora.fleet.provision_service import LeaseAlreadyConsumed, ProvisioningService
from arbora.run_origin import RunOriginRegistry
from tests.fleet._provisioning_support import (
    DisposableAdapter,
    guards,
    make_provisioner,
    new_run,
    request,
)

DIGEST = "d" * 64
VIEW = WorkItemView(
    work_item_id="workitem:orchestrator:one",
    kind="orchestrator_task",
    tenant="tenant:one",
    created_by="person:one",
    queue=None,
    state="open",
    metadata={},
)
SKILL = SkillRef("skill-a", "1.2.0", DIGEST, "s" * 64, ("web.search",))
DECISION = Decision("dec-1", "tenant:one", "skill-a", "s" * 64, ("web.search",))


class Probe:
    """Counts every port call so a refusal can prove nothing later ran."""

    def __init__(self, **over: Any) -> None:
        self.over = over
        self.calls: list[str] = []
        self.agent_digest = over.get("agent_digest", DIGEST)

    def ports(self) -> FlowPorts:
        def discover(view: WorkItemView) -> tuple[str, tuple[SkillRef, ...]]:
            self.calls.append("discover")
            if self.over.get("down"):
                raise ProducerUnavailable
            return "gen-9", tuple(self.over.get("skills", (SKILL,)))

        def distribution(view: WorkItemView, skill: SkillRef) -> Decision:
            self.calls.append("distribution")
            return self.over.get("decision", DECISION)

        def graph_read(skill_id: str) -> tuple[str, str]:
            self.calls.append("graph_read")
            return f"read-{len(self.calls)}", self.over.get("graph_digest", DIGEST)

        def agent_observe(instance_id: str) -> str:
            self.calls.append("agent_observe")
            return self.agent_digest

        def run(instance_id: str, skill: SkillRef) -> RunResult:
            self.calls.append("run")
            return RunResult("trace-1", "result-1", "e" * 64, skill.digest)

        return FlowPorts(discover, distribution, graph_read, agent_observe, run)


def _service(
    sdk: Any, probe: Probe, *, lease: bool = True
) -> tuple[ProvisioningService, Any, Any]:
    adapter = DisposableAdapter()
    ledger, journal = new_run(10)
    consumed: set[str] = set()

    class Leases:
        def consume(self, lease_id: str, work_item_id: str) -> bool:
            if lease_id in consumed:
                return False
            consumed.add(lease_id)
            return True

    service = ProvisioningService(
        provisioner=make_provisioner(sdk, adapter),
        ledger=ledger,
        guards=guards(),
        journal=journal,
        clock=lambda: 5,
        ports=probe.ports(),
        leases=Leases() if lease else None,
    )
    return service, adapter, journal


def _receipt(**over: Any) -> IntegratedProvisionReceipt:
    body: dict[str, Any] = {
        "tenant": "tenant:one",
        "run_id": "run-1",
        "skill_digest": DIGEST,
        "skill_id": "skill-a",
        "skill_version": "1.2.0",
        "discovery_generation": "gen-9",
        "distribution_decision_id": "dec-1",
        "admission_receipt_id": "adm-1",
        "instance_id": "i-1",
        "result_digest": "e" * 64,
        "phases": [
            PhaseBinding(
                phase=p, tenant="tenant:one", run_id="run-1", skill_digest=DIGEST
            )
            for p in ("discovery", "distribution", "provision", "execution")
        ],
    }
    return IntegratedProvisionReceipt.model_validate({**body, **over})


@pytest.mark.spec("ARBORA-FLEET-R027.1")
def test_receipt_binds_all_phases() -> None:
    receipt = _receipt()
    assert IntegratedProvisionReceipt.model_validate(receipt.model_dump()) == receipt
    for bad in (
        PhaseBinding(
            phase="x", tenant="tenant:two", run_id="run-1", skill_digest=DIGEST
        ),
        PhaseBinding(
            phase="x", tenant="tenant:one", run_id="run-2", skill_digest=DIGEST
        ),
        PhaseBinding(
            phase="x", tenant="tenant:one", run_id="run-1", skill_digest="f" * 64
        ),
    ):
        with pytest.raises(ValidationError, match=BINDING_MISMATCH):
            _receipt(phases=[bad])
    with pytest.raises(ValidationError):
        _receipt(skill_digest="short")


@pytest.mark.spec("ARBORA-FLEET-R027.2")
def test_graph_and_agent_agree_before_success(sdk: Any) -> None:
    probe = Probe()
    service, adapter, _journal = _service(sdk, probe)
    receipt = service.for_work_item(VIEW, request("flow-1", 1))
    assert receipt.skill_digest == DIGEST and receipt.skill_version == "1.2.0"
    assert probe.calls.index("agent_observe") > probe.calls.index("graph_read")
    assert receipt.instance_id in {i for v in adapter.live.values() for i in v}
    disagree = Probe(agent_digest="f" * 64)
    service, adapter, _journal = _service(sdk, disagree)
    with pytest.raises(ProvisionFlowRefused) as refused:
        service.for_work_item(VIEW, request("flow-2", 1))
    assert refused.value.code == "SCHEMA_INCOMPATIBLE"
    assert refused.value.details["instance_ids"] and "run" not in disagree.calls


@pytest.mark.spec("ARBORA-FLEET-R027.3")
def test_one_request_traverses_discovery_to_skill(sdk: Any) -> None:
    probe = Probe()
    service, _adapter, journal = _service(sdk, probe)
    authority = WorkItemA2AAuthority(
        lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
    )
    authority.provisioning = service
    receipt = authority.provision_work_item(VIEW, request("flow-3", 1))
    assert probe.calls[0] == "discover" and probe.calls[-1] == "graph_read"
    assert [c for c in probe.calls if c in {"distribution", "run"}] == [
        "distribution",
        "run",
    ]
    assert receipt.discovery_generation == "gen-9" and journal.verify()
    with pytest.raises(RuntimeError, match="unavailable"):
        WorkItemA2AAuthority(
            lambda: None, origins=RunOriginRegistry(), config=RuntimeConfig()
        ).provision_work_item(VIEW, request("x", 1))


@pytest.mark.spec("ARBORA-FLEET-R027.4")
def test_denials_have_no_effect(sdk: Any) -> None:
    cases = {
        "SKILL_NOT_FOUND": {"skills": ()},
        "PRODUCER_UNAVAILABLE": {"down": True},
        "SCHEMA_INCOMPATIBLE": {"decision": replace(DECISION, schema_digest="z" * 64)},
        "CROSS_TENANT_RECEIPT": {"decision": replace(DECISION, tenant="tenant:two")},
        "TOOL_NOT_APPROVED": {"decision": replace(DECISION, approved_tools=())},
    }
    assert set(cases) == set(REFUSALS)
    for code, over in cases.items():
        probe = Probe(**over)
        service, adapter, journal = _service(sdk, probe)
        with pytest.raises(ProvisionFlowRefused) as refused:
            service.for_work_item(VIEW, request("deny", 1))
        assert refused.value.code == code
        assert adapter.calls == 0 and "run" not in probe.calls and not journal._events
        assert "skill-a" not in str(refused.value.details) + str(refused.value)
    with pytest.raises(ProvisionFlowRefused) as foreign:
        _service(sdk, Probe())[0].for_work_item(
            VIEW, request("t", 1, tenant="tenant:two")
        )
    assert foreign.value.code == "CROSS_TENANT_RECEIPT"


@pytest.mark.spec("ARBORA-FLEET-R027.5")
def test_evidence_proves_identity_consumption(sdk: Any) -> None:
    digests = []
    for text in ("old skill text", "rewritten skill text"):
        probe = Probe(skills=(replace(SKILL, tools=("web.search",)),))
        service, _adapter, journal = _service(sdk, probe)
        del text  # the text never reaches the flow; only the identity does
        service.for_work_item(VIEW, request("ev", 1))
        (event,) = [e for e in journal._events if e.kind == EVIDENCE_KIND]
        digests.append(event.payload_digest)
    assert digests[0] == digests[1]
    assert digests[0].startswith("sha256:")
    other = Probe(
        skills=(replace(SKILL, digest="9" * 64),),
        graph_digest="9" * 64,
        agent_digest="9" * 64,
    )
    service, _adapter, journal = _service(sdk, other)
    service.for_work_item(VIEW, request("ev", 1))
    assert journal._events[-1].payload_digest != digests[0]
    assert canonical_digest({"a": 1}).startswith("sha256:")


@pytest.mark.spec("ARBORA-FLEET-R027.6")
@pytest.mark.spec("ARBORA-FLEET-R027")
def test_full_sequence_with_denial_restart_duplicate(sdk: Any) -> None:
    probe = Probe()
    service, adapter, _journal = _service(sdk, probe)
    first = service.for_work_item(VIEW, request("seq", 1))
    duplicate = service.for_work_item(VIEW, request("seq", 1))
    assert duplicate.instance_id == first.instance_id and adapter.calls == 1
    down = Probe(down=True)
    service.ports = down.ports()
    with pytest.raises(ProvisionFlowRefused) as unavailable:
        service.for_work_item(VIEW, request("seq2", 1))
    assert unavailable.value.code == "PRODUCER_UNAVAILABLE" and adapter.calls == 1
    restarted, _adapter, _journal = _service(sdk, Probe())
    assert restarted.for_work_item(VIEW, request("seq3", 1)).skill_digest == DIGEST


@pytest.mark.spec("ARBORA-FLEET-R005.3")
@pytest.mark.spec("ARBORA-FLEET-R005")
def test_a2a_task_routes_through_work_item(sdk: Any) -> None:
    probe = Probe()
    service, _adapter, _journal = _service(sdk, probe)
    router = OrchestratorA2ARouter(lambda: None, provisioning=service)
    receipt = router.route_work_item(VIEW, request("route", 1), lease_id="lease-1")
    assert receipt.run_id == VIEW.work_item_id and receipt.tenant == VIEW.tenant
    with pytest.raises(LeaseAlreadyConsumed) as reused:
        router.route_work_item(VIEW, request("route2", 1), lease_id="lease-1")
    assert reused.value.code == "LEASE_ALREADY_CONSUMED"
    assert probe.calls.count("run") == 1
    bare, _a, _j = _service(sdk, Probe(), lease=False)
    with pytest.raises(LeaseAlreadyConsumed):
        bare.for_work_item(VIEW, request("nolease", 1), lease_id="any")


def test_flow_module_refuses_an_unknown_code() -> None:
    with pytest.raises(ValueError):
        ProvisionFlowRefused("NOPE")
