"""Consumer fixtures only; these do not qualify native persisted authority."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from arbora.identity.composition import build_engine_identity_ports
from arbora.identity.engine import IdentityUnavailable, Resolution

from .test_engine_resolution import resolution_value

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R005.1")


def record_value():
    return {
        "session_ref": "record-reference-fixture",
        "tenant": "fixture-tenant",
        "principal_id": "usr:fixture",
        "absolute_expires_at_ms": 4102444800000,
        "idle_expires_at_ms": 4102444700000,
        "mfa_at_ms": None,
    }


def reply_value():
    return {
        "kind": "resolution",
        "value": {**resolution_value(), "session_record": record_value()},
    }


@pytest.mark.parametrize("field", tuple(record_value()))
def test_record_requires_every_field(field):
    value = reply_value()
    del value["value"]["session_record"][field]
    with pytest.raises(IdentityUnavailable):
        Resolution.from_reply(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tenant", "other"),
        ("principal_id", "other"),
        ("session_ref", ""),
        ("idle_expires_at_ms", True),
        ("absolute_expires_at_ms", -1),
        ("absolute_expires_at_ms", 2**64),
        ("mfa_at_ms", "1"),
    ],
)
def test_record_rejects_mismatch_and_coercion(field, value):
    reply = reply_value()
    reply["value"]["session_record"][field] = value
    with pytest.raises(IdentityUnavailable):
        Resolution.from_reply(reply)


def test_extension_is_explicit_and_legacy_resolution_is_unchanged():
    plain = Resolution.parse(resolution_value())
    bound = Resolution.from_reply(reply_value())
    assert plain == bound
    assert plain.session_record is None
    assert bound.session_record.session_ref == "record-reference-fixture"
    value = reply_value()
    value["value"]["session_record"]["credential"] = "forbidden"
    with pytest.raises(IdentityUnavailable):
        Resolution.from_reply(value)
    value = reply_value()
    value["value"]["unknown"] = True
    with pytest.raises(IdentityUnavailable):
        Resolution.from_reply(value)


class Client:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []

    async def _send(self, method, params, graph, *, idempotency_key=None):
        self.calls.append((method, params, graph, idempotency_key))
        if isinstance(self.reply, Exception):
            raise self.reply
        return deepcopy(self.reply)


@pytest.fixture
def controlled_decoder(monkeypatch):
    # Existing send_identity still validates the real generated request and calls
    # the transport. Only the not-yet-generated response schema is a fixture.
    from epistemic_graph.generated import security

    def decode(result):
        return SimpleNamespace(model_dump=lambda **_: result.payload)

    monkeypatch.setattr(security, "decode_identity", decode)


async def test_existing_generated_resolve_and_fresh_denial(controlled_decoder):
    client = Client(reply_value())
    ports = build_engine_identity_ports(client, graph="identity-fixture")
    state = await ports.sessions.resolve_session("opaque-fixture-session")
    assert state.expires_at_ms == record_value()["idle_expires_at_ms"]
    assert state.session_ref == record_value()["session_ref"]
    assert client.calls == [
        (
            "Identity",
            {
                "op": {
                    "family": "session",
                    "op": "resolve",
                    "request": {"session_token": "opaque-fixture-session"},
                }
            },
            "identity-fixture",
            None,
        )
    ]
    client.reply = PermissionError("revoked")
    with pytest.raises(PermissionError):
        await ports.sessions.resolve_session("opaque-fixture-session")
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "case",
    ["legacy", "bootstrap", "pending", "expired", "future_mfa", "display_handle"],
)
async def test_consumer_refuses_unqualified_records(controlled_decoder, case):
    reply = reply_value()
    if case == "legacy":
        del reply["value"]["session_record"]
    elif case == "bootstrap":
        reply["value"]["is_bootstrap"] = True
    elif case == "pending":
        reply["value"]["session_mfa_pending"] = True
    elif case == "expired":
        reply["value"]["session_record"]["absolute_expires_at_ms"] = 1
    elif case == "future_mfa":
        reply["value"]["session_record"]["mfa_at_ms"] = 4102444800000
    else:
        reply["value"]["session_record"]["session_ref"] = "012345abcdef"
    ports = build_engine_identity_ports(Client(reply), graph="identity-fixture")
    with pytest.raises((IdentityUnavailable, PermissionError)):
        await ports.sessions.resolve_session("opaque-fixture-session")


@pytest.mark.parametrize(
    "credential", ["", "white space", "line\nfeed", "x" * 16385, None]
)
async def test_invalid_credential_never_calls_transport(credential):
    client = Client(reply_value())
    ports = build_engine_identity_ports(client, graph="identity-fixture")
    with pytest.raises(PermissionError):
        await ports.sessions.resolve_session(credential)
    assert not client.calls


async def test_real_generated_legacy_reply_has_no_session_authority():
    from epistemic_graph.generated.security import decode_identity, send_identity

    reply = {"kind": "resolution", "value": resolution_value()}
    client = Client(reply)
    response = await send_identity(
        client,
        {
            "op": {
                "family": "session",
                "op": "resolve",
                "request": {"session_token": "opaque-fixture-session"},
            }
        },
        "identity-fixture",
    )
    decode_identity(response)
    assert Resolution.from_reply(response.payload).session_record is None
    ports = build_engine_identity_ports(client, graph="identity-fixture")
    with pytest.raises(IdentityUnavailable, match="persisted session evidence"):
        await ports.sessions.resolve_session("opaque-fixture-session")


async def test_original_wire_types_are_not_normalized(controlled_decoder):
    reply = reply_value()
    reply["value"]["session_record"]["idle_expires_at_ms"] = "4102444700000"
    ports = build_engine_identity_ports(Client(reply), graph="identity-fixture")
    with pytest.raises(IdentityUnavailable):
        await ports.sessions.resolve_session("opaque-fixture-session")
