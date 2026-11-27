"""The deployment manifest plan and apply through the invoke pipeline (ARBORA-DEPLOY-POLICY-R001 to R003, R007)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_invoke_contract import FixtureRuntime, run, setup_services

from arbora.api.ops import deploy
from arbora.api.registry import Registry
from arbora.security.operation_result import OpError, OpResult
from tests.api._support import make_verified_caller

DIGEST = "registry.example/app/{}@sha256:" + "a" * 64
GRAPH = {
    "shape": {
        "nodes": [
            {
                "node_id": slot,
                "slot": slot,
                "component_id": f"tool:{slot}",
                "tool": {"server": "ops", "tool": f"{slot}_tool"},
            }
            for slot in ("render", "apply")
        ]
    }
}


def _component(cid: str, replicas: int = 2, **over: Any) -> str:
    body = {
        "id": cid,
        "kind": "gateway",
        "image": DIGEST.format(cid),
        "revision": "r1",
        "identity_mode": "none",
        "capacity": {"cpu_millis": 500, "memory_bytes": 1024, "replicas": replicas},
        **over,
    }
    return "\n".join(
        [
            f"  - id: {body['id']}",
            f"    kind: {body['kind']}",
            f"    image: {body['image']}",
            f"    revision: {body['revision']}",
            f"    identity_mode: {body['identity_mode']}",
            *(
                [f"    secrets_ref: {body['secrets_ref']}"]
                if "secrets_ref" in body
                else []
            ),
            "    capacity:",
            "      cpu_millis: 500",
            "      memory_bytes: 1024",
            f"      replicas: {replicas}",
        ]
    )


def _live(cid: str, replicas: int = 2, **over: Any) -> dict[str, Any]:
    return {
        "id": cid,
        "image": DIGEST.format(cid),
        "revision": "r1",
        "replicas": replicas,
        **over,
    }


class Fleet:
    """The bound step pipeline: the live-state read, node calls and deployment.record."""

    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.live = live
        self.calls: list[Any] = []
        self.fail_record = False
        self.write_tool = False

    @property
    def effects(self) -> list[tuple[str, str]]:
        return [(c[1], c[2]) for c in self.calls if c[1] != "live_state"]

    async def __call__(self, op: str, params: Any, caller: Any, key: str) -> Any:
        tool = params.get("tool", op)
        self.calls.append((op, tool, key))
        if op == "deployment.record":
            return (
                OpError("DEPENDENCY_FAILED")
                if self.fail_record
                else OpResult({"value": "ok"})
            )
        if tool == "live_state":
            if self.write_tool:
                return OpResult(code="CONFIRMATION_REQUIRED", details={})
            return OpResult({"value": {"components": self.live}})
        return OpResult({"value": "done"})


class Elevations:
    def __init__(self, **views: Any) -> None:
        self.views = views

    async def list_elevations(self) -> list[Any]:
        return [SimpleNamespace(elevation_id=k, **v) for k, v in self.views.items()]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def assemble(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {"graph": GRAPH}

    monkeypatch.setattr(deploy, "_assemble", assemble)
    root = tmp_path / "manifests"
    root.mkdir()

    def write(components: list[str], name: str = "prod") -> None:
        (root / f"{name}.yaml").write_text(
            f"schema_version: 1\nname: {name}\ncomponents:\n"
            + "\n".join(components)
            + "\n"
        )

    def build(fleet: Fleet, elevations: Elevations | None = None) -> Any:
        ops = {op.id: op for op in deploy.operations()}

        class Runtime(FixtureRuntime):
            service_scopes = frozenset({"example:execute"})

            async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
                handler = {
                    "deploy.manifest.plan": deploy.handle_manifest_plan,
                    "deploy.manifest.apply": deploy.handle_manifest_apply,
                }[op.id]
                shaped = SimpleNamespace(
                    caller=context.caller,
                    client=None,
                    idempotency_key=context.idempotency_key,
                    services={
                        "plan_step_invoke": fleet,
                        "deploy_manifest_root": root,
                        "deploy_config": {
                            "tools": {
                                "live_state": {
                                    "prod": {"server": "ops", "tool": "live_state"}
                                }
                            }
                        },
                        "elevation_service": elevations,
                        "now_ms": lambda: 9_000,
                    },
                )
                return await handler(shaped, params, op)

        registry = Registry([ops["deploy.manifest.plan"], ops["deploy.manifest.apply"]])
        services, *_ = setup_services(
            registry=registry,
            runtime=Runtime(),
            external_effect_operations=frozenset({"deploy.manifest.apply"}),
        )
        fields = {f: getattr(services, f) for f in services.__dataclass_fields__}
        fields["runtime"] = Runtime()
        return type(services)(**fields)

    return SimpleNamespace(write=write, build=build)


CALLER = make_verified_caller("ops:read")


def _caller() -> Any:
    return CALLER(effective_scopes=frozenset({"ops:read", "fleet:control"}))


def _go(services: Any, op_id: str, params: dict[str, Any], **kw: Any) -> Any:
    return run(services, supplied=_caller(), op_id=op_id, params=params, **kw)


PARAMS = {"name": "prod", "target": "prod"}


def _apply(services: Any, plan: Any, key: str, **extra: Any) -> Any:
    params = {**PARAMS, "plan_digest": plan.value["plan_digest"], **extra}
    preview = _go(services, "deploy.manifest.apply", params, idempotency_key=key)
    assert preview.code == "CONFIRMATION_REQUIRED", preview
    return _go(
        services,
        "deploy.manifest.apply",
        params,
        idempotency_key=key,
        plan_ref=preview.details["plan_ref"],
    )


# spec: ARBORA-DEPLOY-POLICY-R001
def test_plan_returns_typed_diff_and_makes_no_write_call(world: Any) -> None:
    world.write([_component("gateway", 3), _component("engine"), _component("webui")])
    fleet = Fleet([_live("gateway", 2), _live("webui"), _live("old-connector")])
    plan = _go(world.build(fleet), "deploy.manifest.plan", PARAMS)

    assert plan.code == "OK"
    entries = plan.value["entries"]
    assert [(e["component_id"], e["change"]) for e in entries] == [
        ("engine", "create"),
        ("gateway", "scale"),
        ("old-connector", "destroy"),
    ]
    assert entries[1]["from"]["replicas"] == 2 and entries[1]["to"]["replicas"] == 3
    assert plan.value["plan_digest"].startswith("sha256:")
    # Only the one live-state read ran: no write, no assembly, no record.
    assert [c[:2] for c in fleet.calls] == [("fleet.call", "live_state")]

    fleet.write_tool = True
    refused = _go(world.build(fleet), "deploy.manifest.plan", PARAMS)
    assert refused.code == "LIVE_STATE_TOOL_NOT_READ"


# spec: ARBORA-DEPLOY-POLICY-R002
def test_second_apply_makes_zero_calls_and_returns_identical_receipt(
    world: Any,
) -> None:
    world.write([_component("gateway"), _component("engine")])
    fleet = Fleet([])
    services = world.build(fleet)
    plan = _go(services, "deploy.manifest.plan", PARAMS)
    first = _apply(services, plan, "k1")

    assert first.code == "OK"
    assert [c["outcome"] for c in first.value["components"]] == ["converged"] * 2
    keys = [key for _, key in fleet.effects if key.startswith("deploy:")]
    digest = plan.value["manifest_digest"]
    assert keys == [
        f"deploy:{digest}:{cid}:{slot}"
        for cid in ("engine", "gateway")
        for slot in ("render", "apply")
    ]

    # The live state now matches: the same apply is empty, calls nothing, and
    # returns the receipt the first run returned.
    fleet.live = [_live("gateway"), _live("engine")]
    before = len(fleet.effects)
    again_plan = _go(services, "deploy.manifest.plan", PARAMS)
    assert again_plan.value["entries"] == []
    second = _apply(services, again_plan, "k2")
    assert second.code == "OK" and second.value == first.value
    assert len(fleet.effects) == before


# spec: ARBORA-DEPLOY-POLICY-R002
def test_missing_input_names_the_gap_and_creates_nothing(world: Any) -> None:
    world.write([_component("gateway", identity_mode="local")])
    fleet = Fleet([])
    services = world.build(fleet)
    plan = _go(services, "deploy.manifest.plan", PARAMS)
    refused = _apply(services, plan, "k1")

    assert refused.code == "MANIFEST_INPUT_MISSING"
    assert refused.details["missing"] == ["components.gateway.secrets_ref"]
    assert fleet.effects == []


# spec: ARBORA-DEPLOY-POLICY-R003
def test_manual_replica_change_is_reported_as_drift(world: Any) -> None:
    world.write([_component("gateway", 2)])
    fleet = Fleet([_live("gateway", 5)])
    plan = _go(world.build(fleet), "deploy.manifest.plan", PARAMS)

    assert [d["component_id"] for d in plan.value["drift"]] == ["gateway"]
    assert plan.value["drift"][0]["from"]["replicas"] == 5
    assert plan.value["destructive"] == []
    assert [c[:2] for c in fleet.calls] == [("fleet.call", "live_state")]


# spec: ARBORA-DEPLOY-POLICY-R003
def test_destructive_apply_needs_a_fresh_approval_and_a_stale_one_expires(
    world: Any,
) -> None:
    world.write([_component("gateway")])

    def attempt(elevations: Elevations | None, **extra: Any) -> tuple[Any, Fleet, Any]:
        fleet = Fleet([_live("gateway"), _live("old-connector")])
        services = world.build(fleet, elevations)
        plan = _go(services, "deploy.manifest.plan", PARAMS)
        return _apply(services, plan, f"k-{len(extra)}", **extra), fleet, plan

    refused, fleet, plan = attempt(Elevations())
    assert refused.code == "APPROVAL_REQUIRED" and fleet.effects == []
    just = f"deploy-destructive:{plan.value['destructive_digest']}"
    fresh = {
        "status": "active",
        "approved_at_ms": 1,
        "remaining_ms": 60_000,
        "justification": just,
    }

    ok, fleet, _ = attempt(Elevations(e1=fresh), approval_elevation_id="e1")
    assert ok.code == "OK"
    stale, fleet, _ = attempt(
        Elevations(e1={**fresh, "remaining_ms": 0}), approval_elevation_id="e1"
    )
    assert stale.code == "APPROVAL_EXPIRED" and fleet.effects == []
    wrong, fleet, _ = attempt(
        Elevations(e1={**fresh, "justification": "deploy-destructive:other"}),
        approval_elevation_id="e1",
    )
    assert wrong.code == "APPROVAL_MISMATCH" and fleet.effects == []
    pending, fleet, _ = attempt(
        Elevations(e1={**fresh, "status": "pending", "approved_at_ms": None}),
        approval_elevation_id="e1",
    )
    assert pending.code == "APPROVAL_REQUIRED" and fleet.effects == []


# spec: ARBORA-DEPLOY-POLICY-R007
def test_rollout_records_one_deployment_and_none_on_failure(world: Any) -> None:
    world.write([_component("gateway"), _component("engine")])
    fleet = Fleet([_live("gateway", 5)])
    services = world.build(fleet)
    plan = _go(services, "deploy.manifest.plan", PARAMS)
    done = _apply(services, plan, "k1")

    assert done.code == "OK"
    records = [c for c in fleet.calls if c[0] == "deployment.record"]
    assert [r[2].rsplit(":", 1)[1] for r in records] == ["engine", "gateway"]

    broken = Fleet([_live("gateway", 5)])
    broken.fail_record = True
    services = world.build(broken)
    plan = _go(services, "deploy.manifest.plan", PARAMS)
    refused = _apply(services, plan, "k2")
    assert refused.code == "UNAVAILABLE"
    assert refused.details["components"] == [
        {"id": "engine", "outcome": "unrecorded"},
        {"id": "gateway", "outcome": "unrecorded"},
    ]
