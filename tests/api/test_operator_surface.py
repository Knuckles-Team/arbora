"""Operator, ecosystem and role operations wait on a served capability.

Rows ARBORA-OPS-R913, R914 and R917.1 to R917.2 stay unbound until their surface is served: every named operation is
registered with its scope and params, and refuses ``UNAVAILABLE`` naming the
capability it waits on. Nothing is faked and no local state is kept.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import operator_surface as surface

_OPS = {op.id: op for op in surface.operations()}


def _model(op: Any) -> Any:
    return op.params


def test_every_named_operation_is_registered_once() -> None:
    assert len(_OPS) == len(surface.pending_ids()) == 25
    for expected in (
        "fleet.servers.register",
        "schedule.tasks.read",
        "ops.maintenance.run",
        "workflows.run",
        "commands.autocomplete",
        "resources.spawn",
        "ecosystem.widgets.read",
        "roles.templates.list",
        "roles.instances.create",
    ):
        assert expected in _OPS


@pytest.mark.parametrize("op_id", sorted(_OPS))
async def test_each_operation_refuses_unavailable_naming_what_it_waits_on(
    op_id: str,
) -> None:
    op = _OPS[op_id]
    with pytest.raises(OperationRefused) as refused:
        await surface.handle_pending(None, {}, op)
    assert refused.value.code == "UNAVAILABLE"
    assert "waits on" in str(refused.value.details["reason"])


def test_admin_effects_need_admin_authority_and_params_are_closed() -> None:
    register = _OPS["fleet.servers.register"]
    assert register.effect.value == "admin"
    assert register.principals.value == "human_undelegated"
    assert _OPS["ops.maintenance.run"].scopes == frozenset({"ops:admin"})
    with pytest.raises(ValidationError):
        _model(register)(name="x", config={}, extra=1)
    with pytest.raises(ValidationError):
        _model(_OPS["code.context.read"])(action="other")
    assert _model(_OPS["schedule.tasks.read"]).model_validate({"from": "a", "to": "b"})
