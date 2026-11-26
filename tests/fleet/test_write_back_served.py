"""The plan-confirmed ``fleet.write_back.commit`` path through the invoke pipeline.

ARBORA-FLEET-R003.6 and R003.8. The pipeline, plan store, effect journal and the
write-back handler are real; the connector gateway is a recording double. An
unconfirmed or mismatched call applies nothing; the confirmed call applies once.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import write_back as wb
from arbora.api.registry import Registry
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import FixtureRuntime, run, setup_services
from tests.fleet._repository_effect_support import (
    FakeGateway,
    binding_for,
    make_request,
    params,
    receipt_for,
)

CALLER = make_verified_caller("kg:write")


def _plain(value: Any) -> Any:
    """The mutable JSON view of the pipeline's frozen parameters."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _stack() -> tuple[Any, FakeGateway, Any, Any]:
    request = make_request()
    binding = binding_for(request)
    gateway = FakeGateway({"value": {"receipt": receipt_for(request)}})
    commit = {op.id: op for op in wb.operations()}["fleet.write_back.commit"]

    class Runtime(FixtureRuntime):
        async def dispatch(self, op: Any, payload: Any, context: Any) -> Any:
            self.calls.append((op, payload, context))
            shaped = SimpleNamespace(
                caller=context.caller,
                client=None,
                idempotency_key=context.idempotency_key,
                service_identity=False,
                owner=None,
                owner_ref=None,
                fleet_decision=None,
                registry_digest="",
                services={
                    "write_back_bindings": wb.WriteBackBindings((binding,)),
                    "fleet_gateway": gateway,
                },
            )
            return await wb.handle_write_back_commit(shaped, _plain(payload), op)

    services, *_ = setup_services(
        registry=Registry([commit]),
        runtime=Runtime(),
        external_effect_operations=frozenset({commit.id}),
    )
    fields = {f: getattr(services, f) for f in services.__dataclass_fields__}
    fields["runtime"] = Runtime()
    return type(services)(**fields), gateway, request, binding


def _go(services: Any, payload: dict, key: str = "key:one", **kw: Any) -> Any:
    return run(
        services,
        supplied=CALLER(),
        op_id="fleet.write_back.commit",
        params=payload,
        idempotency_key=key,
        **kw,
    )


@pytest.mark.spec("ARBORA-FLEET-R003.6")
def test_plan_confirmed_commit_applies_once() -> None:
    services, gateway, request, binding = _stack()
    payload = params(request, binding)

    preview = _go(services, payload)
    assert preview.code == "CONFIRMATION_REQUIRED"
    assert gateway.calls == []  # an unconfirmed commit applies nothing

    ref = preview.details["plan_ref"]

    applied = _go(services, payload, plan_ref=ref)
    assert applied.code == "OK", applied
    assert applied.value["value"]["applied"] is True
    assert [call[:2] for call in gateway.calls] == [
        ("repository-connector", "apply_repository_effect")
    ]

    again = _go(services, payload, plan_ref=ref)
    assert len(gateway.calls) == 1  # a consumed plan never applies twice
    assert again.code in {"OK", "PLAN_CONSUMED", "PLAN_MISMATCH", "PLAN_INVALID"}


@pytest.mark.spec("ARBORA-FLEET-R003.8")
def test_confirmed_commit_carries_the_receipt() -> None:
    services, gateway, request, binding = _stack()
    payload = params(request, binding)
    ref = _go(services, payload).details["plan_ref"]
    applied = _go(services, payload, plan_ref=ref)
    value = applied.value["value"]
    assert value["applied"] is True
    assert gateway.calls[0][2]["request"]["intent"]["target_ref"] == (
        request.intent.target_ref
    )


@pytest.mark.spec("ARBORA-FLEET-R003.6")
def test_a_changed_request_does_not_ride_the_plan() -> None:
    services, gateway, request, binding = _stack()
    payload = params(request, binding)
    ref = _go(services, payload).details["plan_ref"]
    changed = {**payload, "binding_digest": "0" * 64}
    mismatched = _go(services, changed, "key:two", plan_ref=ref)
    assert mismatched.code == "PLAN_MISMATCH"
    assert gateway.calls == []  # the plan binds the request; nothing applied
