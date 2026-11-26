"""entity.approve_link over the engine's ApproveEntityLink (ARBORA-OPS-R887)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.invoke.steps import require_scopes
from arbora.api.ops import entity
from arbora.api.registry import AuditClass, Confirm, Effect
from arbora.pin_lag import medulla as pin_lag
from tests.api._engine_double import EngineDouble, context

_OP = entity.operations()[0]
_PAIR = {
    "left": {"application": "twenty", "entity_id": "c-1"},
    "right": {"application": "erpnext", "entity_id": "cust-9"},
}


@pytest.mark.spec("ARBORA-OPS-R887")
def test_the_operation_is_a_destructive_planned_write_without_an_approver_field() -> (
    None
):
    assert _OP.id == "entity.approve_link"
    assert _OP.effect is Effect.DESTRUCTIVE and _OP.confirm is Confirm.PLAN
    assert _OP.audit is AuditClass.IDENTITY_CHAIN
    assert _OP.scopes == frozenset({"query:write"})
    assert entity.EntityApproveLinkParams(**_PAIR)
    with pytest.raises(ValidationError):
        entity.EntityApproveLinkParams(**_PAIR, approver="someone")  # type: ignore[call-arg]
    caller = SimpleNamespace(effective_scopes=frozenset({"query:read"}))
    refused = require_scopes(_OP, caller)
    assert refused is not None and refused.code == "SCOPE_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R887")
async def test_a_pinned_engine_without_the_method_refuses_naming_it() -> None:
    from medulla.generated import METHOD_IDS

    if "ApproveEntityLink" in METHOD_IDS:
        pytest.skip("the pin carries ApproveEntityLink")
    with pytest.raises(OperationRefused) as refused:
        await entity.handle_entity_approve_link(context(), _PAIR, _OP)
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "ApproveEntityLink"


@pytest.mark.spec("ARBORA-OPS-R887")
async def test_approval_receipt_and_refusal_pass_through_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = EngineDouble(monkeypatch)
    known = {("twenty:c-1", "erpnext:cust-9")}

    def approve(p: dict[str, Any]) -> Any:
        pair = (
            f"{p['left']['application']}:{p['left']['entity_id']}",
            f"{p['right']['application']}:{p['right']['entity_id']}",
        )
        if pair not in known:
            raise EngineRefusal("LINK_PROPOSAL_UNKNOWN")
        return {
            "proposal": {
                "left": p["left"],
                "right": p["right"],
                "approved_by": "alice",
            },
            "source": pair[0],
            "target": pair[1],
            "predicate": "owl:sameAs",
        }

    engine.serve("ApproveEntityLink", approve)
    engine.install(pin_lag)
    receipt = await entity.handle_entity_approve_link(context(), _PAIR, _OP)
    assert receipt["value"]["predicate"] == "owl:sameAs"
    assert receipt["value"]["proposal"]["approved_by"] == "alice"
    sent = engine.sent[0]
    assert sent[1] == _PAIR and sent[2] is True
    assert "approver" not in sent[1]
    with pytest.raises(EngineRefusal) as unknown:
        await entity.handle_entity_approve_link(
            context(),
            {
                "left": _PAIR["left"],
                "right": {"application": "gramps", "entity_id": "z"},
            },
            _OP,
        )
    assert unknown.value.code == "LINK_PROPOSAL_UNKNOWN"
