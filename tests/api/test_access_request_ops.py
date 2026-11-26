"""Data-product access requests are admitted against the engine's steward role (ARBORA-OPS-R888)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import governance
from tests.api._engine_double import EngineDouble, context

_OP = next(op for op in governance.operations() if op.id == "access_request.decide")
_PARAMS = {"product": "product:orders", "principal": "person:asker", "approve": True}


class _Catalog:
    """The engine's roles for one product and its access-request decision."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, stewards: list[str]) -> None:
        self.grants: list[dict[str, Any]] = []
        self.engine = EngineDouble(monkeypatch)
        self.engine.serve(
            "CatalogRead",
            lambda p: {
                "governance": {"owners": ["person:owner"], "stewards": stewards}
            },
        )
        self.engine.serve("AccessRequestDecide", self._decide)
        self.engine.install(governance)

    def _decide(self, p: dict[str, Any]) -> Any:
        self.grants.append(p)
        return {"decision": "approved" if p["approve"] else "denied"}


@pytest.mark.spec("ARBORA-OPS-R888")
async def test_access_request_approval_needs_steward(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _Catalog(monkeypatch, stewards=["principal:one"])
    answer = await governance.handle_access_request_decide(context(), _PARAMS, _OP)
    assert answer["decision"] == "approved"
    assert catalog.grants == [{**_PARAMS, "tenant": "tenant:one"}]

    # A caller the engine does not name as owner or steward leaves zero effects.
    outsider = _Catalog(monkeypatch, stewards=["person:other"])
    with pytest.raises(OperationRefused) as refused:
        await governance.handle_access_request_decide(context(), _PARAMS, _OP)
    assert refused.value.code == "STEWARD_ROLE_MISSING"
    assert outsider.grants == []
    assert {m for m, _, _ in outsider.engine.sent} == {"CatalogRead"}


@pytest.mark.spec("ARBORA-OPS-R888")
def test_the_operation_is_a_native_replayed_write_with_no_role_table() -> None:
    assert governance.NATIVE_IDEMPOTENCY["access_request.decide"] == (
        "medulla:AccessRequestDecide"
    )
    assert _OP.scopes == frozenset({"approvals:decide"})
