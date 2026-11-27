"""The SAML ACS served over the real auth app (ARBORA-IDENTITY-R012.2.3.2, R012.2.3.1).

The response reader and verifier are injected (no XML or crypto lives in Arbora); the
engine is a structural double of ``register_assertion``, which the pinned release lacks.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from arbora.identity.federated import (
    SAML_ACS,
    EngineSamlReplayStore,
    ReadSamlResponse,
)
from arbora.identity.saml import ParsedSamlAssertion
from tests.identity._federated_fixture import (
    NOW,
    cookie_values,
    post_form,
)
from tests.identity.test_modes_and_browser import ORIGIN

UNIFORM = b'{"error":"Authentication required"}'


def _assertion(assertion_id: str = "_a1", **changes: object) -> ParsedSamlAssertion:
    fields: dict[str, object] = {
        "assertion_id": assertion_id,
        "issuer": "https://idp.invalid",
        "subject": "alice-name-id",
        "audiences": ("urn:arbora:sp",),
        "recipient": ORIGIN + SAML_ACS,
        "not_before": NOW - timedelta(minutes=1),
        "not_on_or_after": NOW + timedelta(minutes=5),
    }
    return ParsedSamlAssertion(**{**fields, **changes})  # type: ignore[arg-type]


def _read(
    assertion: ParsedSamlAssertion,
    *,
    signatures: int = 1,
    signed_is_read: bool = True,
    role: str = "staff",
) -> ReadSamlResponse:
    return ReadSamlResponse(assertion, {"role": (role,)}, signatures, signed_is_read)


def _post(world: SimpleNamespace, name: str, read: ReadSamlResponse):
    world.reader.responses[name] = read
    return post_form(world, SAML_ACS, {"SAMLResponse": name})


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.3.2")
def test_signed_assertion_yields_principal_and_each_attack_is_refused(
    world: SimpleNamespace,
) -> None:
    reader = world.reader
    status, headers, _ = _post(world, "ok", _read(_assertion()))
    # A cross-site form post from the provider opens the session for the mapped identity.
    assert status == 303 and dict(headers)["location"] == ORIGIN + "/"
    assert cookie_values(headers)
    assert world.engine.logins[0]["idp_id"] == "saml1"
    assert world.engine.logins[0]["subject"] == "alice-name-id"
    assert world.engine.logins[0]["claims"] == {"role": ["staff"]}
    assert world.engine.assertions == {
        "_a1": int((NOW + timedelta(minutes=5)).timestamp() * 1000)
    }
    attacks = {
        "SAML_UNSIGNED": _read(_assertion("_b"), signatures=0),
        "SAML_SIGNATURE_WRAPPING": _read(_assertion("_c"), signatures=2),
        "SAML_SIGNATURE_WRAPPING ": _read(_assertion("_d"), signed_is_read=False),
        "SAML_AUDIENCE_MISMATCH": _read(_assertion("_e", audiences=("urn:other",))),
        "SAML_EXPIRED": _read(
            _assertion(
                "_f",
                not_on_or_after=NOW - timedelta(seconds=1),
                not_before=NOW - timedelta(minutes=5),
            )
        ),
    }
    for code, read in attacks.items():
        status, headers, body = _post(world, code, read)
        assert (status, body) == (401, UNIFORM) and not cookie_values(headers)
        assert world.audit[-1]["outcome"] == code.strip()
    # A bad signature is refused before the replay store records the id.
    reader.valid = False
    status, _, body = _post(world, "bad", _read(_assertion("_h")))
    assert (status, body) == (401, UNIFORM)
    assert world.audit[-1]["outcome"] == "SAML_SIGNATURE_INVALID"
    assert "_h" not in world.engine.assertions
    reader.valid = True
    # The same assertion id twice: the engine refuses the second as a replay.
    status, headers, body = post_form(world, SAML_ACS, {"SAMLResponse": "ok"})
    assert (status, body) == (401, UNIFORM) and not cookie_values(headers)
    assert world.audit[-1]["outcome"] == "SAML_REPLAYED"
    assert len(world.engine.logins) == 1


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.3.2")
def test_acs_fails_closed_without_a_reader(readerless: SimpleNamespace) -> None:
    assert post_form(readerless, SAML_ACS, {"SAMLResponse": "x"})[0] == 503
    assert not readerless.engine.calls and not readerless.engine.logins


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.3.2")
def test_unknown_recipient_and_unreadable_response_are_refused_alike(
    world: SimpleNamespace,
) -> None:
    reader = world.reader
    other = _read(_assertion("_z", recipient="https://other.invalid/acs"))
    status, _, body = _post(world, "other", other)
    assert (status, body) == (401, UNIFORM)
    assert post_form(world, SAML_ACS, {"SAMLResponse": "garbage"})[0] == 401
    assert reader.verified == 0 and not world.engine.logins


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.3.1")
def test_engine_replay_store_registers_by_assertion_id_with_its_expiry(
    world: SimpleNamespace,
) -> None:
    import asyncio

    store = EngineSamlReplayStore(object(), "fixture-graph", "saml1")
    expires = NOW + timedelta(minutes=5)
    assert asyncio.run(store.register("_r1", expires)) is True
    assert asyncio.run(store.register("_r1", expires)) is False
    assert asyncio.run(store.register("_r2", expires)) is True
    assert world.engine.assertions["_r1"] == int(expires.timestamp() * 1000)


@pytest.mark.pin_lag
@pytest.mark.spec("ARBORA-IDENTITY-R012.2.3.2")
def test_pinned_release_without_register_assertion_answers_unavailable(
    lagged: SimpleNamespace,
) -> None:
    status, _, body = _post(lagged, "ok", _read(_assertion()))
    assert status == 503 and b"IdentityOpTokenRegisterAssertion" in body
    assert not lagged.engine.logins and not lagged.engine.assertions
