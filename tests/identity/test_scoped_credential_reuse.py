"""Served scoped credential reuse (ARBORA-IDENTITY-R023.3 to R023.6)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.access.credential_use import (
    CredentialRefusal,
    CredentialRefused,
    resolve_credential,
)
from tests.identity._credential_fixture import CODE, TOKEN, VERIFIER, Ports, request


def _run(ports: Ports, **kwargs: Any) -> dict[str, Any]:
    async def go() -> dict[str, Any]:
        record = dict(await resolve_credential(request(**kwargs), ports))
        ports.use_backend(record)  # the backend is touched only after resolution
        return record

    return asyncio.run(go())


def _refusal(ports: Ports, **kwargs: Any) -> CredentialRefusal:
    with pytest.raises(CredentialRefused) as caught:
        _run(ports, **kwargs)
    assert str(caught.value) == caught.value.refusal.value  # code text only
    return caught.value.refusal


@pytest.mark.spec(
    "ARBORA-IDENTITY-R023.3", "ARBORA-IDENTITY-R023.4", "ARBORA-IDENTITY-R023.6"
)
@pytest.mark.parametrize("profile", ["jwt_oidc", "opaque"])
def test_served_pkce_reuse_and_refusal(profile: str) -> None:
    ports = Ports(profile=profile)
    ports.callback = {
        "state": "state-123456",
        "token": {"access": TOKEN, "refresh": "r1"},
    }
    record = _run(ports)
    # The token is persisted through the vault before the caller reads it, then reused.
    assert ports.vault["vault:ada:idp"]["access"] == TOKEN and record["access"] == TOKEN
    assert ports.decisions == 2  # a fresh engine authorization resumed the operation
    assert _run(ports)["access"] == TOKEN
    # A granted second client succeeds; an unrelated one has no grant and no backend effect.
    ungranted = Ports(granted=False)
    effects = ungranted.backend_calls
    assert _refusal(ungranted) is CredentialRefusal.REUSE_DENIED
    assert ungranted.backend_calls == effects
    cases = {
        CredentialRefusal.INTERACTION_REQUIRED: {"interactive": False},
        CredentialRefusal.STATE_MISMATCH: {
            "callback": {"state": "forged", "token": {"access": TOKEN}}
        },
        CredentialRefusal.PERSISTENCE_FAILED: {
            "callback": {"state": "state-123456", "token": {"access": TOKEN}},
            "persist_fails": True,
        },
    }
    for expected, setup in cases.items():
        refused = Ports(profile=profile)
        for key, value in setup.items():
            setattr(refused, key, value)
        assert _refusal(refused) is expected
        # A refused ceremony leaves no usable grant and never touches the backend.
        assert refused.backend_calls == 0 and not refused.vault
    # An unanswered challenge times out on the injected deadline, not on a sleep.
    timed_out = Ports(profile=profile)
    assert _refusal_fast(timed_out) is CredentialRefusal.INPUT_TIMEOUT
    assert timed_out.backend_calls == 0 and not timed_out.vault
    # A replayed callback state never dispatches the waiting operation.
    replay = Ports(profile=profile)
    replay.callback = {"state": "old-state", "token": {"access": TOKEN}}
    assert (
        _refusal(replay) is CredentialRefusal.STATE_MISMATCH
        and replay.backend_calls == 0
    )


def _refusal_fast(ports: Ports) -> CredentialRefusal:
    async def go() -> CredentialRefusal:
        original = ports.complete

        async def quick(challenge: Any, timeout: float) -> Any:
            raise TimeoutError

        ports.complete = quick  # type: ignore[method-assign]
        try:
            await resolve_credential(request(), ports)
        except CredentialRefused as refused:
            return refused.refusal
        finally:
            ports.complete = original  # type: ignore[method-assign]
        raise AssertionError("expected a refusal")

    return asyncio.run(go())


@pytest.mark.spec("ARBORA-IDENTITY-R023.5")
def test_evidence_fields_and_redaction() -> None:
    ports = Ports()
    ports.callback = {
        "state": "state-123456",
        "token": {"access": TOKEN, "refresh": "r1"},
    }
    _run(ports)
    [event] = ports.events
    assert (
        event["principal"] == "bob"
        and event["tenant"] == "t1"
        and event["client"] == "cli-b"
    )
    assert event["provider"] == "idp" and event["scopes"] == "mail.read"
    assert (
        event["resource"] == "mailbox:ada" and event["engine_decision_ref"] == "dec-1"
    )
    assert (
        event["vault_receipt"] == "receipt:vault:ada:idp"
        and event["use"] == "cross_agent"
    )
    assert event["challenge_ref"] and event["issuance"]
    blob = repr(ports.events)
    for secret in (TOKEN, CODE, VERIFIER, "r1"):
        assert secret not in blob
    # A refusal is recorded too, with its code and still no secret.
    denied = Ports(granted=False)
    with pytest.raises(CredentialRefused):
        _run(denied)
    assert denied.events[0]["refusal_code"] == "REUSE_DENIED"
