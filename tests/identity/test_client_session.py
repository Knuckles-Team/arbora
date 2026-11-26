"""The client session record (ARBORA-IDENTITY-R026.1)."""

from __future__ import annotations

import pytest

from arbora.identity.client_session import ClientSession, ClientSessionRefused
from arbora.security.actor_context import ActorContext, ActorType


@pytest.mark.spec("ARBORA-IDENTITY-R026.1")
def test_fields_required_and_unsigned_principal_rejected() -> None:
    actor = ActorContext(
        actor_id="person:ada",
        actor_type=ActorType.HUMAN,
        tenant_id="tenant:one",
        authenticated=True,
    )
    record = ClientSession.from_verified_caller(actor, session_id="s1", now_ms=5)
    assert record.node_id == "client_session:tenant:one:s1"
    assert record.properties()["principal"] == "person:ada"
    unsigned = ActorContext(actor_id="person:ada", tenant_id="tenant:one")
    with pytest.raises(ClientSessionRefused) as refused:
        ClientSession.from_verified_caller(unsigned, session_id="s1")
    assert refused.value.code == "CLIENT_SESSION_UNSIGNED_PRINCIPAL"
    with pytest.raises(ClientSessionRefused) as missing:
        ClientSession.from_verified_caller(actor, session_id="")
    assert missing.value.code == "CLIENT_SESSION_FIELD_MISSING"
