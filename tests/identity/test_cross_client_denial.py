"""Cross-client denial is uniform (ARBORA-IDENTITY-R026.7)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.identity import handle_clients_get
from tests.identity._session_fakes import Engine, caller, context


@pytest.mark.spec("ARBORA-IDENTITY-R026.7")
def test_foreign_and_unknown_return_identical_denials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Engine().install(monkeypatch)
    engine.add_session("person:bob", "b1")
    ctx = context(caller("person:ada"))

    def denial(session_id: str) -> tuple[str, dict]:
        with pytest.raises(OperationRefused) as refused:
            asyncio.run(handle_clients_get(ctx, {"session_id": session_id}, None))
        return refused.value.code, dict(refused.value.details)

    assert denial("b1") == denial("nope") == ("CLIENT_NOT_FOUND", {})
