"""Logout and session revocation stop live run leases (ARBORA-IDENTITY-R029.5)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops.identity import handle_sessions_revoke
from arbora.security.run_authority import CallerRevocations
from arbora.security.session_revocation import SessionNotOwned, revoke_own_session
from tests.identity._session_fakes import Engine, caller, context


class _Lease:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def revoke(self, reason: str) -> None:
        self.reasons.append(reason)


@pytest.mark.spec("ARBORA-IDENTITY-R029.5")
def test_logout_and_revocation_stop_leases(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = CallerRevocations()
    leases = [_Lease(), _Lease()]
    actor = SimpleNamespace(tenant_id="tenant:one", actor_id="person:ada")
    for lease in leases:
        registry.register(actor, lease)  # type: ignore[arg-type]
    events: list = []
    stopped = revoke_own_session(
        tenant="tenant:one",
        caller_principal="person:ada",
        session_principal="person:ada",
        session_id="s1",
        emit=events.append,
        registry=registry,
    )
    assert stopped == 2 and all(lease.reasons == ["logout"] for lease in leases)
    assert len(events) == 1 and events[0]["leases_stopped"] == "2"
    with pytest.raises(SessionNotOwned):  # logout reaches only the caller's own session
        revoke_own_session(
            tenant="tenant:one",
            caller_principal="person:ada",
            session_principal="person:bob",
            session_id="s2",
            emit=events.append,
            registry=registry,
        )
    # The operation: administrator scope, a named session, one audit event.
    engine = Engine().install(monkeypatch)
    monkeypatch.setattr("arbora.api.ops.identity.engine_send", engine.send)
    engine.add_session("person:bob", "b1")
    monkeypatch.setattr("arbora.api.ops.identity.CALLER_REVOCATIONS", registry)
    bob = SimpleNamespace(tenant_id="tenant:one", actor_id="person:bob")
    lease = _Lease()
    registry.register(bob, lease)  # type: ignore[arg-type]
    admin = context(caller("person:root", scopes=frozenset({"identity:admin"})))
    params = {"session_id": "b1", "reason": "operator_revoked"}
    done = asyncio.run(handle_sessions_revoke(admin, params, None))
    assert done["value"]["leases_stopped"] == 1 and lease.reasons == [
        "operator_revoked"
    ]
    assert len(admin.events) == 1
    plain = context(caller("person:ada"))
    with pytest.raises(OperationRefused) as scope:
        asyncio.run(handle_sessions_revoke(plain, params, None))
    assert scope.value.code == "SCOPE_MISSING"
    with pytest.raises(OperationRefused) as foreign:
        asyncio.run(
            handle_sessions_revoke(admin, {"session_id": "none", "reason": "x"}, None)
        )
    assert foreign.value.code == "SESSION_NOT_OWNED"
