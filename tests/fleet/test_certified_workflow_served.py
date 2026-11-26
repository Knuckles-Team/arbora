"""Certified workflow admission through the served ``agents.run`` path.

ARBORA-FLEET-R034.8 to R034.12, R034.14, R034.15, R019.1 and R021.1. The engine is
a structural double speaking the ``DecisionEval`` receipt read; everything else
(the invoke pipeline, the admission gate, the policy gate and the multiplexer
binding) is the real code.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from arbora.api.registry import Surface
from arbora.fleet.admission import validate_certification
from arbora.fleet.admission_models import AdmissionRefused, parse_certification_binding
from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.workflow_binding import WorkflowBindings
from tests.fleet._certified_support import (
    World,
    attestation,
    certification,
    h,
    no_pin_lag,
    workflow_metadata,
)

pytestmark = pytest.mark.usefixtures("pinned_engine")


@pytest.fixture
def pinned_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    no_pin_lag(monkeypatch)


def _world_with_chain(**kw) -> tuple[World, dict]:
    """An atomic skill, a composed skill over it, both attested in the engine."""
    world = World(**kw)
    world.engine.store(attestation("atom"))
    world.engine.store(attestation("composed", deps=("atom",)))
    metadata = workflow_metadata(
        dependencies={"composed": ("atom",), "atom": ()},
        certifications=(
            certification("composed", deps=("atom",)),
            certification("atom"),
        ),
    )
    return world, {"workflow": metadata}


@pytest.mark.spec("ARBORA-FLEET-R034.8")
def test_stale_dependency_or_denied_tool_blocks_all() -> None:
    world, metadata = _world_with_chain()
    stale = {"workflow": {**metadata["workflow"]}}
    stale["workflow"]["certifications"] = [
        certification("composed", deps=("atom",)),
        certification("atom", version="v2"),  # the engine holds v1
    ]
    refused = world.call(stale)
    assert refused.code == "PRODUCER_UNAVAILABLE", (
        refused.details
    )  # no attestation stored for v2 digest
    assert world.runtime.calls == []

    world.engine.store(attestation("atom", version="v2"))
    world.engine.stored[h("attestation:atom:v2")]["body"]["pins"]["model"][
        "definition_digest"
    ] = h("model:r2")
    refused = world.call(stale, key="k2")
    assert refused.code == "CERTIFICATION_STALE"
    assert world.runtime.calls == []

    denied, metadata = _world_with_chain(denied=frozenset({"crm.read"}))
    assert denied.call(metadata).code == "WORKFLOW_NOT_ADMITTED"
    assert denied.runtime.calls == []

    ok, metadata = _world_with_chain()
    assert ok.call(metadata).code == "OK"
    assert len(ok.runtime.calls) == 1


@pytest.mark.spec("ARBORA-FLEET-R034.9")
@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
def test_agents_run_reaches_gate_before_execution(surface: Surface) -> None:
    world, metadata = _world_with_chain()
    assert world.call(metadata, surface=surface).code == "OK"
    order = world.journal
    assert order[-1] == "run"
    assert order.index("engine:DecisionEval") < order.index("run")
    assert order.count("run") == 1


@pytest.mark.spec("ARBORA-FLEET-R034.10")
def test_refusal_codes_and_no_effect() -> None:
    world, metadata = _world_with_chain()
    required = world.call({"skill": {"id": "s", "text": "ok", "certification": {}}})
    assert required.code == "CERTIFICATION_REQUIRED"

    missing = {"workflow": {**metadata["workflow"], "certifications": []}}
    assert world.call(missing, key="a").code == "CERTIFICATION_REQUIRED"

    cyclic = workflow_metadata(dependencies={"a": ("b",), "b": ("a",)})
    assert world.call({"workflow": cyclic}, key="b").code == "WORKFLOW_NOT_ADMITTED"

    world.grant["revision"] = "g2"  # the grant revision moved after the binding
    assert world.call(metadata, key="c").code == "WORKFLOW_NOT_ADMITTED"
    world.grant["revision"] = "g1"

    unwired = World(wired=False)
    cert = {"skill": {"id": "s", "text": "ok", "certification": certification("s")}}
    assert unwired.call(cert).code == "PRODUCER_UNAVAILABLE"

    unknown = workflow_metadata(tools={"crm.write": h("crm.write")})
    assert world.call({"workflow": unknown}, key="d").code == "WORKFLOW_NOT_ADMITTED"
    assert world.runtime.calls == [] and unwired.runtime.calls == []


@pytest.mark.spec("ARBORA-FLEET-R034.11")
def test_trace_orders_atomic_before_composed() -> None:
    world, metadata = _world_with_chain()
    assert world.call(metadata).code == "OK"
    outcome = world.audit.events[-1]
    assert outcome["admission.certification_order"] == "atom,composed"
    receipts = outcome["admission.certification_receipts"].split(",")
    assert receipts == [h("attestation:atom:v1"), h("attestation:composed:v1")]
    assert outcome["admission.workflow_snapshot_digest"] == h("workflow")
    assert outcome["admission.grant_revision"] == "g1"


@pytest.mark.spec("ARBORA-FLEET-R034.12")
@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
def test_allowed_denied_stale(surface: Surface) -> None:
    allowed, metadata = _world_with_chain()
    assert allowed.call(metadata, surface=surface).code == "OK"
    assert len(allowed.runtime.calls) == 1

    denied, metadata = _world_with_chain(grant_tools=())
    assert denied.call(metadata, surface=surface).code == "WORKFLOW_NOT_ADMITTED"

    stale, metadata = _world_with_chain()
    stale.engine.stored[h("attestation:atom:v1")]["body"]["pins"]["model"][
        "definition_digest"
    ] = h("model:other")
    assert stale.call(metadata, surface=surface).code == "CERTIFICATION_STALE"
    assert denied.runtime.calls == [] and stale.runtime.calls == []


@pytest.mark.spec("ARBORA-FLEET-R034.14")
def test_attestation_consumed_by_digest() -> None:
    from arbora.fleet.engine_admission import flatten_attestation

    item = attestation("s", deps=("d",))
    binding = parse_certification_binding(certification("s", deps=("d",)))
    assert validate_certification(binding, flatten_attestation(item)) == h(
        "attestation:s:v1"
    )
    for field_name in (
        "skill_id",
        "skill_version",
        "model_endpoint_revision",
        "system_prompt_digest",
        "attestation_digest",
    ):
        flat = {**flatten_attestation(item), field_name: "changed"}
        with pytest.raises(AdmissionRefused) as caught:
            validate_certification(binding, flat)
        assert caught.value.code == "CERTIFICATION_STALE"
        assert caught.value.detail["fields"] == [field_name]
    flat = {**flatten_attestation(item), "dependency_digests": (h("x"),)}
    with pytest.raises(AdmissionRefused):
        validate_certification(binding, flat)


@pytest.mark.spec("ARBORA-FLEET-R034.15")
def test_attestation_matrix() -> None:
    def skill(world: World) -> dict:
        return {"skill": {"id": "s", "text": "ok", "certification": certification("s")}}

    world = World()
    assert world.call(skill(world), key="1").code == "PRODUCER_UNAVAILABLE"  # missing
    world.engine.store(attestation("s", version="v0"))
    assert world.call(skill(world), key="2").code == "PRODUCER_UNAVAILABLE"
    stale = attestation("s")
    stale["body"]["pins"]["criteria_policy"]["definition_digest"] = h("policy:old")
    world.engine.store(stale)
    assert world.call(skill(world), key="3").code == "CERTIFICATION_STALE"
    world.engine.store(attestation("s"))
    assert world.call(skill(world), key="4").code == "OK"
    assert len(world.runtime.calls) == 1  # only the exact attestation ran


@pytest.mark.spec("ARBORA-FLEET-R019.1")
def test_workflow_admission_uses_policy_gate() -> None:
    world, metadata = _world_with_chain(denied=frozenset({"crm.read"}))
    refused = world.call(metadata)
    assert refused.code == "WORKFLOW_NOT_ADMITTED"
    assert refused.details["reason"] == "policy denied"
    assert world.policy.asked == [["fleet:tool:crm.read"]]  # the one narrowing path
    assert world.runtime.calls == []

    class Broken(type(world.policy)):  # type: ignore[misc]
        async def loadable(self, items, caller):
            raise RuntimeError("policy service down")

    allowed, metadata = _world_with_chain()
    services = allowed.services
    fields = {f: getattr(services, f) for f in services.__dataclass_fields__}
    fields["policy_gate"] = Broken()
    allowed.services = type(services)(**fields)
    assert allowed.call(metadata, key="b").code == "POLICY_UNAVAILABLE"


@pytest.mark.spec("ARBORA-FLEET-R021.1")
def test_revoked_grant_stops_later_calls() -> None:
    revision = {"value": "g1"}
    bindings = WorkflowBindings()
    bindings.declare({"crm.read"})
    world, metadata = _world_with_chain()
    assert world.call(metadata).code == "OK"
    from arbora.fleet.admission import WorkflowAdmission

    bindings.bind(
        "session",
        WorkflowAdmission(("atom", "composed"), ("crm.read",), h("workflow"), "g1"),
        lambda: revision["value"],
    )
    mux = SimpleNamespace(
        workflow_bindings=bindings,
        _global_visible={"crm.read", "other"},
        _local_gated=set(),
    )
    dispatch = lambda name: MCPMultiplexer.tool_dispatchable(  # noqa: E731
        mux, name, session_key="session"
    )
    assert dispatch("crm.read")  # first call passes
    assert bindings.refusal("crm.read", "other-session") == "WORKFLOW_NOT_ADMITTED"
    revision["value"] = "g2"  # the grant was revoked
    assert not dispatch("crm.read")
    assert bindings.refusal("crm.read", "session") == "WORKFLOW_GRANT_REVOKED"
    assert dispatch("other")  # a tool that is not a workflow tool is unaffected
