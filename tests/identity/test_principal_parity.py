"""User and service account parity (ARBORA-IDENTITY-R026.6)."""

from __future__ import annotations

import asyncio

import pytest

from arbora.api.ops.identity import handle_clients_list
from tests.identity._session_fakes import Engine, caller, context


@pytest.mark.spec("ARBORA-IDENTITY-R026.6")
def test_equal_catalog_and_visible_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = Engine().install(monkeypatch)
    engine.add_session("person:ada", "a1")
    engine.add_session("service:ci", "c1")
    params = {"limit": 100, "cursor": None}
    scopes = frozenset({"identity:self"})
    user = caller("person:ada", scopes=scopes, principal_kind="human")
    service = caller("service:ci", scopes=scopes, principal_kind="service")
    one = asyncio.run(handle_clients_list(context(user), params, None))
    two = asyncio.run(handle_clients_list(context(service), params, None))
    assert len(one["value"]["clients"]) == len(two["value"]["clients"]) == 1
    # The catalog filter reads scopes and tenant only, never the principal kind.
    import inspect

    from arbora.api.mcp import discovery

    assert "principal_kind" not in inspect.getsource(discovery)
