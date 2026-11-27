"""Component choices and target profiles drive the confirmed steps (ARBORA-DEPLOY-R013.6).

Each case builds a confirmed inventory, asks the engine's graph shape for one node
per ``deploy`` component, and runs it through the bound invoke pipeline
(``run_confirmed_steps``) with recorded fleet tools. The evidence per case is the
exact set of component effects, the resource identities, the profile digest, the
readiness and the reused services that survive untouched. A refused plan makes zero
effects.
"""

from __future__ import annotations

import asyncio
import itertools
import random
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.deployment.assembly import graph_digest, run_confirmed_steps
from arbora.deployment.component_choices import (
    ChoiceInventory,
    ChoiceRefused,
    build_inventory,
)
from arbora.deployment.genesis_environments import (
    ORCHESTRATOR_TARGET,
    PRIVILEGES,
    RUNTIMES,
    target_cell_refusal,
)
from arbora.security.operation_result import OpResult

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R013.6")

SEED = 4110
INVENTORIES = 32
COMPONENTS = (
    "engine",
    "gateway",
    "connector-fleet",
    "web-ui",
    "vault",
    "identity",
    "cache",
)
FREE_ORCHESTRATOR = {"standalone": "bare-metal", "helm": "kubernetes"}
NAMED_ORCHESTRATORS = {
    ("compose", "docker"): "docker-compose",
    ("compose", "podman"): "podman-compose",
    ("swarm", "docker"): "docker-swarm",
}


class Recorder:
    """The bound pipeline: records every effect; a reused service must never appear."""

    def __init__(self) -> None:
        self.effects: list[tuple[str, str]] = []

    async def __call__(self, op: str, params: Any, caller: Any, key: str) -> Any:
        self.effects.append((params["server"], params["tool"]))
        return OpResult({"value": params["tool"]})


class Probe:
    def __init__(self, unreachable: frozenset[str] = frozenset()) -> None:
        self.unreachable = unreachable
        self.probed: list[str] = []

    async def __call__(self, component: str) -> bool:
        self.probed.append(component)
        return component not in self.unreachable


def graph_for(inventory: ChoiceInventory) -> dict[str, Any]:
    """The engine graph: one node per component that is to be deployed."""
    return {
        "shape": {
            "nodes": [
                {
                    "node_id": f"apply-{component}",
                    "slot": "apply",
                    "component_id": component,
                    "tool": {"server": "ops", "tool": f"apply_{component}"},
                }
                for component in inventory.placement_components()
            ]
        }
    }


def answers(rng: random.Random) -> list[dict[str, Any]]:
    rows = []
    for index, component in enumerate(COMPONENTS):
        action = rng.choice(("deploy", "reuse"))
        rows.append(
            {
                "component_id": component,
                "action": action,
                "endpoint_ref": f"endpoint://reused/{index}"
                if action == "reuse"
                else None,
                "profile": "standalone",
                "confirmed": True,
            }
        )
    return rows


def run_case(
    inventory: ChoiceInventory, probe: Probe, recorder: Recorder
) -> dict[str, Any]:
    graph = graph_for(inventory)
    return asyncio.run(
        run_confirmed_steps(
            recorder,
            "caller",
            graph,
            plan_digest=graph_digest(graph),
            reuse=inventory.reuse_components,
            probe_reused=probe,
        )
    )


def test_seed_4110_inventories_and_matrix_cells() -> None:
    rng = random.Random(SEED)
    mixed = 0
    for _ in range(INVENTORIES):
        inventory = build_inventory(COMPONENTS, answers(rng))
        recorder, probe = Recorder(), Probe()
        outcome = run_case(inventory, probe, recorder)
        mixed += bool(inventory.deploy_components and inventory.reuse_components)

        # Exact component effects: one apply per deployed component, in order.
        assert outcome["ok"] is True
        assert [tool for _, tool in recorder.effects] == [
            f"apply_{c}" for c in inventory.deploy_components
        ]
        # Resource identities: one idempotency key per deployed component.
        assert [s["idempotency_key"].rsplit(":", 1)[1] for s in outcome["steps"]] == [
            f"apply-{c}" for c in inventory.deploy_components
        ]
        # Readiness: every reused service is probed once, read-only; none is touched.
        assert sorted(probe.probed) == sorted(inventory.reuse_components)
        assert not any(
            tool == f"apply_{c}"
            for _, tool in recorder.effects
            for c in inventory.reuse_components
        )
        assert inventory.choices_digest.startswith("sha256:")
    assert mixed >= 8, "the seeded draw must exercise mixed inventories"

    # Every target cell has a case: success cells deploy, refused cells make no effect.
    inventory = build_inventory(
        COMPONENTS,
        [
            {
                "component_id": c,
                "action": "deploy",
                "endpoint_ref": None,
                "profile": "standalone",
                "confirmed": True,
            }
            for c in COMPONENTS
        ],
    )
    cases = 0
    for target in sorted(set(ORCHESTRATOR_TARGET.values())):
        for runtime, privilege in itertools.product(RUNTIMES, PRIVILEGES):
            orchestrator = FREE_ORCHESTRATOR.get(target) or NAMED_ORCHESTRATORS.get(
                (target, runtime)
            )
            if orchestrator is None:
                continue  # no orchestrator runs this target on this runtime
            cases += 1
            recorder = Recorder()
            refusal = target_cell_refusal(orchestrator, runtime, privilege)
            if refusal is None:
                assert run_case(inventory, Probe(), recorder)["ok"] is True
                assert len(recorder.effects) == len(COMPONENTS)
            else:
                assert refusal.startswith("unsupported_target")
                assert recorder.effects == []  # a refused cell never reaches a step
    assert cases >= 12


def test_refused_plans_make_zero_effects() -> None:
    reuse_two = build_inventory(
        COMPONENTS,
        [
            {
                "component_id": c,
                "action": "reuse" if c in ("vault", "cache") else "deploy",
                "endpoint_ref": f"endpoint://{c}" if c in ("vault", "cache") else None,
                "profile": "standalone",
                "confirmed": True,
            }
            for c in COMPONENTS
        ],
    )
    # An unreachable reused service refuses and never falls back to deploying it.
    recorder = Recorder()
    with pytest.raises(OperationRefused) as down:
        run_case(reuse_two, Probe(frozenset({"vault"})), recorder)
    assert down.value.code == "REUSE_UNREACHABLE" and recorder.effects == []

    # A graph that is not the confirmed one makes no effect.
    graph = graph_for(reuse_two)
    with pytest.raises(OperationRefused) as altered:
        asyncio.run(
            run_confirmed_steps(
                recorder,
                "caller",
                graph,
                plan_digest=graph_digest({**graph, "extra": 1}),
            )
        )
    assert altered.value.code == "PLAN_MISMATCH" and recorder.effects == []

    # An injected node for a reused component makes no effect either.
    injected = {
        "shape": {
            "nodes": [
                {
                    "node_id": "update-vault",
                    "slot": "apply",
                    "component_id": "vault",
                    "tool": {"server": "ops", "tool": "apply_vault"},
                }
            ]
        }
    }
    with pytest.raises(OperationRefused) as mutation:
        asyncio.run(
            run_confirmed_steps(
                recorder,
                "caller",
                injected,
                plan_digest=graph_digest(injected),
                reuse=reuse_two.reuse_components,
            )
        )
    assert mutation.value.code == "REUSE_COMPONENT_MUTATION"
    assert recorder.effects == []

    # An inventory with a missing choice is refused before any graph exists.
    with pytest.raises(ChoiceRefused) as missing:
        build_inventory(COMPONENTS, [])
    assert missing.value.code == "CHOICE_MISSING"
