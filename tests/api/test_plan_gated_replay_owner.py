"""Every plan-gated operation settles a confirmed replay through one owner.

ARBORA-OPS-R007.2: a consumed plan cannot prove that its
effect completed, so the effect journal is the single coordinator. Fleet,
admin and console operations reach it by the same rule, with no second path.
"""

from __future__ import annotations

import pytest
from test_invoke_contract import operation, run, setup_services

from arbora.api.ops import get_registry
from arbora.api.registry import Confirm, Effect, Registry, Surface


@pytest.mark.spec("ARBORA-OPS-R007.2")
def test_every_registered_plan_gated_operation_is_covered_by_the_journal() -> None:
    registry = get_registry()
    gated = {op.id for op in registry if op.confirm in (Confirm.PLAN, Confirm.CONSOLE)}
    assert {"dashboard.delete", "project.mode.set", "identity.users.disable"} <= gated
    services, *_ = setup_services(
        registry=registry,
        external_effect_operations=frozenset(),
        fleet_effect=lambda *_: None,
        schema_validate=lambda *_: None,
    )
    assert gated <= services.external_effect_operations


@pytest.mark.spec("ARBORA-OPS-R007.2")
def test_a_plan_gated_operation_replays_once_without_its_own_declaration() -> None:
    registry = Registry([operation(effect=Effect.DESTRUCTIVE)])
    services, runtime, _, journal, _ = setup_services(
        registry=registry, external_effect_operations=frozenset()
    )
    preview = run(services)
    assert preview.code == "CONFIRMATION_REQUIRED" and not runtime.calls
    ref = preview.details["plan_ref"]
    first = run(services, plan_ref=ref)
    assert first.code == "OK"
    assert run(services, plan_ref=ref) == first
    assert len(runtime.calls) == 1 and len(journal.rows) == 1


@pytest.mark.spec("ARBORA-OPS-R007.2")
def test_a_console_operation_uses_the_same_owner() -> None:
    registry = Registry([operation(effect=Effect.ADMIN, surfaces=frozenset(Surface))])
    services, *_ = setup_services(
        registry=registry, external_effect_operations=frozenset()
    )
    assert services.external_effect_operations == frozenset({"example.write"})


@pytest.mark.spec("ARBORA-OPS-R007.2")
def test_without_the_journal_a_plan_gated_operation_fails_closed() -> None:
    registry = Registry([operation(effect=Effect.DESTRUCTIVE)])
    services, runtime, _, _, _ = setup_services(
        registry=registry, effects=None, external_effect_operations=frozenset()
    )
    refused = run(services)
    assert refused.code == "UNAVAILABLE" and not runtime.calls
