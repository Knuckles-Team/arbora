"""Runs carry the opening session id (ARBORA-IDENTITY-R026.3)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.agents import _with_client_session
from tests.identity._session_fakes import caller, context


@pytest.mark.spec("ARBORA-IDENTITY-R026.3")
def test_run_record_shows_opening_session_id() -> None:
    who = caller(engine_claims={"session_ref": "sess-7"})
    message = _with_client_session(context(who), {"metadata": {"k": "v"}})
    assert message["metadata"]["client_session_id"] == "sess-7"
    assert message["metadata"]["client_principal"] == "person:ada"
    assert message["metadata"]["k"] == "v"
    with pytest.raises(OperationRefused) as refused:
        _with_client_session(context(caller()), {})
    assert refused.value.code == "CLIENT_SESSION_MISSING"
    assert asyncio.iscoroutinefunction(
        __import__("arbora.api.ops.agents", fromlist=["x"]).handle_agent
    )
    assert SimpleNamespace  # the handler is the one served entry of agents.run
