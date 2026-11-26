"""Exit obligation ``write_back_receipt``: a confirmed commit returns the journal receipt (ARBORA-RELEASE-R003.6)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.ops import write_back as wb
from tests.fleet._repository_effect_support import (
    FakeGateway,
    binding_for,
    context,
    make_request,
    params,
    receipt_for,
)


class JournalGateway(FakeGateway):
    """The engine effect journal in front of the connector, keyed by approval."""

    def __init__(self, result: Any) -> None:
        super().__init__(result)
        self.journal: dict[str, Any] = {}
        self.effects = 0

    async def call(self, caller, server, tool, arguments, **kw):
        key = kw["approval_ref"]
        if key not in self.journal:
            self.effects += 1
            self.journal[key] = await super().call(
                caller, server, tool, arguments, **kw
            )
        return self.journal[key]


@pytest.mark.spec("ARBORA-RELEASE-R003.6")
async def test_confirmed_commit_returns_the_engine_journal_receipt() -> None:
    request = make_request()
    binding = binding_for(request)
    gateway = JournalGateway({"value": {"receipt": receipt_for(request)}})
    ctx = context(
        bindings=wb.WriteBackBindings((binding,)), gateway=gateway, key="plan:ref-1"
    )
    (preview_op,) = [o for o in wb.operations() if o.id.endswith("preview")]
    (commit_op,) = [o for o in wb.operations() if o.id.endswith("commit")]

    shown = await wb.handle_write_back_preview(
        ctx, params(request, binding), preview_op
    )
    assert shown["value"]["applied"] is False
    assert gateway.effects == 0

    first = await wb.handle_write_back_commit(ctx, params(request, binding), commit_op)
    assert first["value"]["applied"] is True
    assert first["value"]["request_digest"] == request.change_set.change_set_digest
    # The receipt is the one the journal holds under the confirmed plan reference.
    held = gateway.journal["ref-1"]["value"]["receipt"]
    assert first["value"]["receipt"]["idempotency_key"] == held["idempotency_key"]

    again = await wb.handle_write_back_commit(ctx, params(request, binding), commit_op)
    assert again["value"] == first["value"]
    assert gateway.effects == 1
