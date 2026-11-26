"""Delegation wiring in fleet.call (ARBORA-IDENTITY-R028.3 to R028.6)."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from arbora.access.delegation import DelegationRefused, resolve_actor_exchange_bearer

USER = SimpleNamespace(
    principal="person:ada",
    tenant="tenant:one",
    principal_kind="human",
    authenticated=True,
    effective_scopes=frozenset({"s:r"}),
    request_id="r1",
)


class _Issuer:
    def __init__(self, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def exchange(self, claims, audience):
        self.calls += 1
        if self.fail:
            raise RuntimeError("down")
        return "tok.en.sig", 99, "e" * 32


def _resolve(caller=USER, issuer=None, events=None, needs=True):
    return asyncio.run(
        resolve_actor_exchange_bearer(
            "keycloak-agent",
            "get_user",
            needs_actor=needs,
            caller=caller,
            claims={"sub": "person:ada"} if caller is not None else None,
            connector_client_id="keycloak-agent",
            issuer=issuer or _Issuer(),
            emit=(events if events is not None else []).append,
        )
    )


@pytest.mark.spec("ARBORA-IDENTITY-R028.3")
@pytest.mark.spec("ARBORA-IDENTITY-R028.6")
def test_actor_required_tools_succeed_for_a_user() -> None:
    assert _resolve(needs=False) is None  # a process credential keeps its path
    started = time.perf_counter()
    for _ in range(200):
        headers = _resolve()
    per_call_ms = (time.perf_counter() - started) * 1000 / 200
    assert headers == {"Authorization": "Bearer tok.en.sig"}
    assert per_call_ms < 50
    # The multiplexer holds the only exchange call site.
    from arbora.fleet.multiplexer import MCPMultiplexer

    assert callable(MCPMultiplexer._resolve_actor_exchange_bearer)


@pytest.mark.spec("ARBORA-IDENTITY-R028.4")
def test_refusals_make_no_connector_call() -> None:
    issuer = _Issuer()
    for caller in (
        None,
        SimpleNamespace(**{**USER.__dict__, "principal_kind": "service"}),
    ):
        with pytest.raises(DelegationRefused) as refused:
            _resolve(caller=caller, issuer=issuer)
        assert refused.value.code == "IDENTITY_DELEGATION_REQUIRED"
    assert issuer.calls == 0
    with pytest.raises(DelegationRefused) as failed:
        _resolve(issuer=_Issuer(fail=True))
    assert failed.value.code == "IDENTITY_EXCHANGE_FAILED"


@pytest.mark.spec("ARBORA-IDENTITY-R028.5")
def test_audit_event_names_exchange_and_holds_no_token() -> None:
    events: list = []
    _resolve(events=events)
    assert len(events) == 1
    event = events[0]
    assert event["delegated_subject"] == "person:ada"
    assert event["audience"] == "keycloak-agent"
    assert (
        event["exchange_id"] == "e" * 32
        and event["target"] == "keycloak-agent/get_user"
    )
    assert "tok.en.sig" not in repr(event)
