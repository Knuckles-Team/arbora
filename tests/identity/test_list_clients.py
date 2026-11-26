"""Caller-scoped client listing (ARBORA-IDENTITY-R026.5)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.ops.identity import handle_clients_list
from tests.identity._session_fakes import Engine, caller, context


@pytest.mark.spec("ARBORA-IDENTITY-R026.5")
def test_user_sees_own_admin_sees_all(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine().install(monkeypatch)
    engine.add_session("person:ada", "a1")
    engine.add_session("person:bob", "b1")
    engine.add_session("person:eve", "e1", tenant="tenant:two")
    params = {"limit": 100, "cursor": None}

    def listed(who):
        out = asyncio.run(handle_clients_list(context(who), params, None))
        return sorted(row["session_id"] for row in out["value"]["clients"])

    assert listed(caller("person:ada")) == ["a1"]
    admin = caller("person:root", scopes=frozenset({"identity:admin"}))
    assert listed(admin) == ["a1", "b1"]
    reader = caller("person:ada", scopes=frozenset({"identity:read"}))
    assert listed(reader) == ["a1"]
