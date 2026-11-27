"""The multiplexer child call path asks the credential authority first (ARBORA-OPS-R893)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.fleet import credential_admission as admission
from arbora.fleet.credential_admission import CredentialRefusal, credentialed_call
from arbora.gateway.credential_authority import CredentialAuthority
from tests.gateway._sdk_binding_double import PROFILE, decision, install
from tests.gateway.test_credential_authority import _Grants, _Receipts

_CFG = {"credential_grant": PROFILE}


def _session() -> Any:
    actor = SimpleNamespace(
        authenticated=True, actor_id="person:caller", client_id="client:one"
    )
    return SimpleNamespace(actor=actor, tenant="tenant:one")


@pytest.mark.spec("ARBORA-OPS-R893")
async def test_a_credentialed_child_call_runs_under_the_verified_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = install(monkeypatch)
    monkeypatch.setattr("arbora.security.session.current_session", _session)
    receipts = _Receipts()
    authority = CredentialAuthority(_Grants(decision()), receipts, lambda: 1_000)
    async with credentialed_call(authority, "mail-agent", "send_mail", _CFG):
        bound = module.current_credential_binding()
        assert bound is not None and bound.grant_ref == "grant:1"
    assert module.current_credential_binding() is None
    assert [r["outcome"] for r in receipts.rows] == ["ok"]


@pytest.mark.spec("ARBORA-OPS-R893")
async def test_a_refused_or_unbound_authority_never_reaches_the_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)
    monkeypatch.setattr("arbora.security.session.current_session", _session)
    authority = CredentialAuthority(
        _Grants(decision(allow=False, reason_code="RESOURCE_GRANT_MISSING")),
        _Receipts(),
        lambda: 1_000,
    )
    with pytest.raises(CredentialRefusal) as missing:
        async with credentialed_call(authority, "mail-agent", "send_mail", _CFG):
            raise AssertionError("child reached")
    assert missing.value.code == "binding_mismatch"
    with pytest.raises(CredentialRefusal) as unbound:
        async with credentialed_call(None, "mail-agent", "send_mail", _CFG):
            raise AssertionError("child reached")
    assert unbound.value.code == "authority_unavailable"
    # A child that declares no credential grant is not gated.
    async with credentialed_call(None, "plain", "tool", {}):
        pass
    assert admission.CredentialRefusal is CredentialRefusal
