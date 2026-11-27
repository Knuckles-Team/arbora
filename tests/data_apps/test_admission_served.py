"""Application admission runs one at a time and rolls back (ARBORA-DATA-MARKET-R004).

A fake engine holds the run nodes, the tenant slot and the governed changes, so a
second handler call, as after a restart, sees only what the engine stored.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import msgpack
import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import apps
from arbora.api.ops.ports import PORTS_BINDING, OperationPorts
from arbora.api.ops.registry_factory import get_registry
from arbora.api.registry import Confirm, Idempotency
from arbora.control_plane.admission import AdmissionState
from arbora.control_plane.admission import port as admission_port
from arbora.control_plane.admission.engine import SOURCE_METHODS, EngineAdmissionSources
from arbora.control_plane.admission.port import ControlPlaneAppAdmission
from arbora.control_plane.admission.run import TRANSITIONS

NO_OP: Any = None
BASE: dict[str, Any] = {
    "app_name": "ledger",
    "source_id": "ledger-new",
    "source_dialect": "postgres",
    "source_version": "16",
    "tables": ["accounts", "entries"],
    "prior_connector": "ledger-old",
    "credentials_scope": ["t1"],
    "tenant_scope": ["t1"],
    "mapping_approved": True,
    "conformance_checked": True,
    "rollback_proven": True,
    "connector_overlap": ["ledger-old"],
    "approve": False,
    "dry_run": False,
}


class FakeEngine:
    """Nodes, one governed-change table and the source methods, with injectable faults."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.changes: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.counts = {"ledger-new": {"accounts": 5, "entries": 9}}
        self.counts["ledger-old"] = dict(self.counts["ledger-new"])
        self.digests = {"ledger-new": "d1", "ledger-old": "d1"}
        self.healthy = True
        self.fault: str | None = None

    def sent(self, method: str) -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]

    async def send(
        self, _context: Any, method: str, params: dict[str, Any], **_k: Any
    ) -> Any:
        self.calls.append((method, params))
        if self.fault == method:
            self.fault = None
            raise RuntimeError("injected fault")
        return getattr(self, "_" + method)(params)

    def _CreateNodeIfAbsent(self, p: dict[str, Any]) -> None:
        self.nodes.setdefault(p["node_id"], msgpack.unpackb(p["properties_msgpack"]))

    def _GetNodePropertiesBatch(self, p: dict[str, Any]) -> list[Any]:
        return [[n, self.nodes.get(n)] for n in p["node_ids"]]

    def _CompareAndSetNodeFields(self, p: dict[str, Any]) -> bool:
        node = self.nodes[p["node_id"]]
        for key, value in msgpack.unpackb(p["conditions_msgpack"]).items():
            if node.get(key) != value:
                return False
        node.update(msgpack.unpackb(p["updates_msgpack"]))
        return True

    def _GovernedChange(self, p: dict[str, Any]) -> Any:
        op = p["op"]
        if op["op"] == "propose":
            assert op["request"]["kind"] == "governed.source-authority"
            self.changes[op["request"]["change_id"]] = "proposed"
        elif op["op"] == "approve":
            self.changes[op["request"]["change_id"]] = "approved"
        elif op["op"] == "get":
            if op["change_id"] not in self.changes:
                raise EngineRefusal("NOT_FOUND")
            return {"status": self.changes[op["change_id"]]}
        return {}

    def _ProbeForeignSource(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"ok": self.healthy}

    def _GetForeignSource(self, p: dict[str, Any]) -> dict[str, Any]:
        return {"name": p["name"]}

    def _ForeignSourceDetach(self, p: dict[str, Any]) -> None:
        return None

    def _ForeignSourceParity(self, p: dict[str, Any]) -> dict[str, Any]:
        return {
            "counts": self.counts[p["name"]],
            "sample_digest": self.digests[p["name"]],
        }


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> FakeEngine:
    fake = FakeEngine()
    monkeypatch.setattr(admission_port, "engine_send", fake.send)
    monkeypatch.setattr(admission_port, "guard", lambda *_m, **_k: None)
    monkeypatch.setattr(admission_port, "parity_sample", lambda: 100)
    return fake


def context(tenant: str = "t1", key: str = "k1") -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant=tenant, principal="p"),
        idempotency_key=key,
        services={PORTS_BINDING: OperationPorts(apps=ControlPlaneAppAdmission())},
    )


async def admit(engine_: FakeEngine, **over: Any) -> dict[str, Any]:
    out = await apps.handle_apps_admit(context(), {**BASE, **over}, NO_OP)
    return out["value"]


def detached(engine_: FakeEngine) -> list[str]:
    """The sources detached, one entry each (a table's registration is ``<source>.<table>``)."""
    names = [p["name"].split(".")[0] for p in engine_.sent("ForeignSourceDetach")]
    return list(dict.fromkeys(names))


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_failed_admission_rolls_back_without_retiring_prior_connector(
    engine: FakeEngine,
) -> None:
    planned = await admit(engine)
    assert planned["state"] == "planned"  # waits for the governed approval
    engine.fault = "ProbeForeignSource"  # a crash while probing
    with pytest.raises(RuntimeError):
        await admit(engine, approve=True)
    run = engine.nodes[planned["run_id"]]
    assert run["state"] == "rolled_back"
    assert run["failure"] == "probe_error"
    assert detached(engine) == ["ledger-new"]  # the prior connector is untouched
    assert engine.nodes["admission-active:t1"]["holder"] == ""
    # A restart replays the same request: it reads the stored terminal state and moves nothing.
    again = await admit(engine, approve=True)
    assert again["state"] == "rolled_back"
    assert detached(engine) == ["ledger-new"]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_a_crash_while_probing_resumes_from_the_engine_state(
    engine: FakeEngine,
) -> None:
    planned = await admit(engine)
    engine.changes[planned["change_id"]] = "approved"
    engine.nodes[planned["run_id"]]["state"] = "probing"  # died after the move
    resumed = await admit(engine)
    assert resumed["state"] == "admitted"
    assert resumed["parity"]["passed"] is True


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_parity_failure_rolls_back_and_names_the_tables(
    engine: FakeEngine,
) -> None:
    engine.counts["ledger-new"]["entries"] = 8
    await admit(engine)
    with pytest.raises(OperationRefused) as refused:
        await admit(engine, approve=True)
    assert refused.value.code == "PARITY_FAILED"
    assert detached(engine) == ["ledger-new"]
    stored = next(n for n in engine.nodes.values() if n.get("type") == "AdmissionRun")
    assert stored["state"] == "rolled_back"
    assert stored["parity"]["tables"]["entries"] == {"new": 8, "prior": 9}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_a_sample_digest_mismatch_fails_parity(engine: FakeEngine) -> None:
    engine.digests["ledger-new"] = "other"
    await admit(engine)
    with pytest.raises(OperationRefused) as refused:
        await admit(engine, approve=True)
    assert refused.value.code == "PARITY_FAILED"
    assert refused.value.details["parity"]["sample"]["limit"] == 100


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_an_unhealthy_probe_rolls_back(engine: FakeEngine) -> None:
    engine.healthy = False
    await admit(engine)
    with pytest.raises(OperationRefused) as refused:
        await admit(engine, approve=True)
    assert refused.value.code == "UNAVAILABLE"
    assert detached(engine) == ["ledger-new"]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_a_second_run_for_the_tenant_conflicts_until_the_first_ends(
    engine: FakeEngine,
) -> None:
    first = await admit(engine)
    with pytest.raises(OperationRefused) as refused:
        await admit(engine, app_name="crm", source_id="crm-new")
    assert refused.value.code == "CONFLICT"
    # The same plan again is a replay, never a conflict.
    assert (await admit(engine))["run_id"] == first["run_id"]
    done = await admit(engine, approve=True)
    assert done["state"] == "admitted"
    other = await admit(engine, app_name="crm", source_id="crm-new")
    assert other["state"] == "planned"


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_a_blocked_plan_opens_no_run(engine: FakeEngine) -> None:
    with pytest.raises(OperationRefused) as refused:
        await admit(engine, rollback_proven=False)
    assert refused.value.code == "POLICY_DENIED"
    assert "rollback is unproven" in refused.value.details["blockers"]
    assert engine.nodes == {}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_dry_run_lists_source_mapping_and_parity_tables(
    engine: FakeEngine,
) -> None:
    out = await admit(engine, dry_run=True)
    assert out["parity_tables"] == ["accounts", "entries"]
    assert out["plan"]["ready"] is True
    assert out["source_id"] == "ledger-new"
    assert engine.nodes == {} and engine.changes == {}


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_retire_needs_admitted_parity_and_its_own_approval(
    engine: FakeEngine,
) -> None:
    planned = await admit(engine)
    ctx = context()
    with pytest.raises(OperationRefused) as early:
        await apps.handle_apps_retire_connector(
            ctx, {"run_id": planned["run_id"], "approve": True}, NO_OP
        )
    assert early.value.code == "CONFLICT"  # not admitted yet
    await admit(engine, approve=True)
    with pytest.raises(OperationRefused) as unapproved:
        await apps.handle_apps_retire_connector(
            ctx, {"run_id": planned["run_id"], "approve": False}, NO_OP
        )
    assert unapproved.value.code == "POLICY_DENIED"
    assert "ledger-old" not in detached(engine)
    done = await apps.handle_apps_retire_connector(
        ctx, {"run_id": planned["run_id"], "approve": True}, NO_OP
    )
    assert done["value"]["state"] == "retired"
    assert detached(engine) == ["ledger-old"]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_a_foreign_tenant_run_answers_like_an_absent_one(
    engine: FakeEngine,
) -> None:
    planned = await admit(engine)
    with pytest.raises(OperationRefused) as refused:
        await apps.handle_apps_retire_connector(
            context("t2"), {"run_id": planned["run_id"], "approve": True}, NO_OP
        )
    absent = None
    with pytest.raises(OperationRefused) as missing:
        await apps.handle_apps_retire_connector(
            context("t2"), {"run_id": "admission-run:t2:none", "approve": True}, NO_OP
        )
    assert (refused.value.code, refused.value.details) == (
        missing.value.code,
        missing.value.details,
    )
    assert absent is None


@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
def test_the_state_machine_is_closed() -> None:
    assert set(TRANSITIONS) == set(AdmissionState)
    assert {s.value for s in AdmissionState} == {
        "planned",
        "approved",
        "probing",
        "admitted",
        "rolled_back",
        "retired",
    }
    assert TRANSITIONS[AdmissionState.ROLLED_BACK] == frozenset()
    assert TRANSITIONS[AdmissionState.RETIRED] == frozenset()
    assert AdmissionState.PROBING not in TRANSITIONS[AdmissionState.PLANNED]
    assert AdmissionState.RETIRED not in TRANSITIONS[AdmissionState.PROBING]


@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
def test_the_operations_are_served_as_plans_with_a_replay_owner() -> None:
    registry = get_registry()
    for op_id in ("apps.admit", "apps.retire_connector"):
        spec = registry.get(op_id)
        assert spec is not None
        assert spec.confirm is Confirm.PLAN
        assert spec.idempotency is Idempotency.KEY_REQUIRED
        assert spec.scopes == frozenset({"federation:admin"})
        assert op_id in apps.NATIVE_IDEMPOTENCY


@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
def test_parity_sample_reads_data_apps_config() -> None:
    from arbora.control_plane.admission.engine import parity_sample

    assert parity_sample("data_apps:\n  parity_sample: 50\n") == 50
    assert parity_sample("data_apps: {}\n") == 1000
    for bad in (9, 100001, "x"):
        with pytest.raises(ValueError):
            parity_sample({"data_apps": {"parity_sample": bad}})


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_attach_reads_each_table_registration_and_detach_removes_each(
    engine: FakeEngine,
) -> None:
    await admit(engine)
    await admit(engine, approve=True)
    asked = [p["name"] for p in engine.sent("GetForeignSource")]
    assert asked == ["ledger-new.accounts", "ledger-new.entries"]
    assert engine.sent("ForeignSourceAdmit") == []
    sources = EngineAdmissionSources(lambda m, p: engine.send(None, m, p))
    await sources.detach("ledger-new", ["accounts", "same"])
    await sources.detach("same", ["same"])
    assert [p["name"] for p in engine.sent("ForeignSourceDetach")][-3:] == [
        "ledger-new.accounts",
        "ledger-new.same",
        "same",
    ]


@pytest.mark.asyncio
@pytest.mark.spec("ARBORA-DATA-MARKET-R004")
async def test_the_pinned_release_refuses_detach_and_parity_naming_the_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    ids = generated.METHOD_IDS
    lacking = (
        {k: v for k, v in ids.items() if k not in SOURCE_METHODS}
        if isinstance(ids, dict)
        else frozenset(ids) - set(SOURCE_METHODS)
    )
    monkeypatch.setattr(generated, "METHOD_IDS", lacking)
    from arbora.api.ops.pin_guards import guard

    for method in SOURCE_METHODS:
        with pytest.raises(OperationRefused) as refused:
            guard(method)
        assert refused.value.code == "UNAVAILABLE"
        assert method in str(refused.value.details)
