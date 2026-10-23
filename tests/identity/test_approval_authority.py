"""Supplied-owner contract tests, not durable restart or served approval proof."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from arbora.identity.approval import (
    HumanSnapshotCheck as Check,
)
from arbora.identity.approval import (
    OriginalHumanBinding,
    OriginalSessionSnapshot,
    RegisteredApprovalKey,
    evaluate_human_snapshot,
)
from arbora.identity.engine import IdentityUnavailable, Resolution, SessionRecord
from arbora.identity.ports import SessionState

pytestmark = pytest.mark.spec("ARBORA-A2A-R005.2.3")


@pytest.fixture
def supplied():
    resolution = Resolution(
        "human:one",
        "human",
        "human",
        "active",
        False,
        (),
        (),
        ("tool:write",),
        False,
        False,
        False,
        {
            "principal": "human:one",
            "tenant": "tenant:one",
            "audience": "arbora",
            "agent_id": "human:one",
            "roles": [],
            "scopes": ["tool:write"],
            "policy_version": "policy:1",
            "delegation": [],
        },
    )
    current = SessionState(resolution, 3000, "session:original", None)
    session = OriginalSessionSnapshot(
        SessionRecord("session:original", "tenant:one", "human:one", 3000, 3000, None),
        ("tool:write",),
        1,
    )
    key = RegisteredApprovalKey(
        "key:one",
        "tenant:one",
        "human:one",
        1,
        "ab" * 32,
        3000,
        None,
        "signed_approval",
    )
    binding = OriginalHumanBinding(
        "session:original",
        "tenant:one",
        "human:one",
        1,
        "key:one",
        1,
        2000,
    )
    return SimpleNamespace(binding=binding, session=session, key=key, current=current)


class Sessions:
    def __init__(self, current):
        self.current = current
        self.calls = 0

    async def resolve_session(self, credential):
        assert credential == "synthetic-credential"
        self.calls += 1
        return self.current


class References:
    def __init__(self, *snapshots):
        self.snapshots = iter(snapshots)
        self.seen = []

    async def resolve_original_session(self, reference):
        self.seen.append(reference)
        value = next(self.snapshots)
        if isinstance(value, Exception):
            raise value
        return value


class Keys:
    def __init__(self, *snapshots):
        self.snapshots = iter(snapshots)
        self.seen = []

    async def resolve_key(self, reference, *, principal_id, purpose):
        self.seen.append((reference, principal_id, purpose))
        value = next(self.snapshots)
        if isinstance(value, Exception):
            raise value
        return value


async def evaluate(supplied, *, session=None, key=None, now=1500):
    return await evaluate_human_snapshot(
        supplied.binding,
        credential="synthetic-credential",
        sessions=Sessions(supplied.current),
        references=References(supplied.session, session or supplied.session),
        keys=Keys(supplied.key, key or supplied.key),
        clock_ms=lambda: now,
    )


async def test_current_snapshot_is_not_consumed_and_rechecks_every_time(supplied):
    for _ in range(2):
        assert await evaluate(supplied) == Check.CURRENT_SNAPSHOT_ONLY
    assert supplied.binding.approval_expires_at_ms == 2000


@pytest.mark.parametrize("missing", ["sessions", "references", "keys"])
async def test_missing_owner_after_reconstruction_is_unavailable(supplied, missing):
    ports = {
        "sessions": Sessions(supplied.current),
        "references": References(supplied.session),
        "keys": Keys(supplied.key),
    }
    ports[missing] = None
    assert (
        await evaluate_human_snapshot(
            supplied.binding,
            credential="synthetic-credential",
            **ports,
            clock_ms=lambda: 1500,
        )
        == Check.AUTHORITY_UNAVAILABLE
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("human", "human:other"),
        ("tenant", "tenant:other"),
        ("session_ref", "session:other"),
        ("authority_revision", 2),
        ("key_id", "key:other"),
        ("key_revision", 2),
    ],
)
async def test_exact_original_binding_required(supplied, field, value):
    supplied.binding = replace(supplied.binding, **{field: value})
    assert await evaluate(supplied) == Check.BINDING_MISMATCH


@pytest.mark.parametrize("owner", ["key", "session"])
async def test_revocation_observed_on_second_read_refuses(supplied, owner):
    changed = (
        replace(supplied.key, revoked_at_ms=1400)
        if owner == "key"
        else PermissionError()
    )
    assert await evaluate(supplied, **{owner: changed}) == Check.AUTHORITY_REFUSED


@pytest.mark.parametrize("owner", ["key", "session"])
async def test_revision_race_refuses(supplied, owner):
    changed = (
        replace(supplied.key, revision=2)
        if owner == "key"
        else replace(supplied.session, authority_revision=2)
    )
    assert await evaluate(supplied, **{owner: changed}) == Check.BINDING_MISMATCH


async def test_key_material_change_without_revision_refuses(supplied):
    assert (
        await evaluate(supplied, key=replace(supplied.key, public_key_hex="cd" * 32))
        == Check.SNAPSHOT_CHANGED
    )


async def test_session_scope_change_without_revision_refuses(supplied):
    session = replace(supplied.session, scopes=("tool:read",))
    assert await evaluate(supplied, session=session) == Check.BINDING_MISMATCH


@pytest.mark.parametrize("now", [2000, 3000])
async def test_expired_approval_refuses(supplied, now):
    assert await evaluate(supplied, now=now) == Check.EXPIRED


@pytest.mark.parametrize("owner", ["key", "session"])
async def test_source_expiry_refuses(supplied, owner):
    if owner == "key":
        supplied.key = replace(supplied.key, expires_at_ms=1500)
    else:
        supplied.session = replace(
            supplied.session,
            record=replace(supplied.session.record, idle_expires_at_ms=1500),
        )
    assert await evaluate(supplied) == Check.EXPIRED


async def test_source_renewal_does_not_extend_approval(supplied):
    supplied.session = replace(
        supplied.session,
        record=replace(
            supplied.session.record,
            idle_expires_at_ms=9000,
            absolute_expires_at_ms=9000,
        ),
    )
    supplied.key = replace(supplied.key, expires_at_ms=9000)
    assert await evaluate(supplied, now=2000) == Check.EXPIRED
    assert supplied.binding.approval_expires_at_ms == 2000


@pytest.mark.parametrize("kind,bootstrap", [("service", False), ("human", True)])
async def test_process_or_bootstrap_authority_is_not_human_approval(
    supplied, kind, bootstrap
):
    resolution = replace(supplied.current.resolution, kind=kind, is_bootstrap=bootstrap)
    supplied.current = replace(supplied.current, resolution=resolution)
    assert await evaluate(supplied) == Check.AUTHORITY_REFUSED


@pytest.mark.parametrize("field", ["tenant", "principal_id"])
async def test_foreign_registered_key_owner_refuses(supplied, field):
    supplied.key = replace(supplied.key, **{field: "other"})
    assert await evaluate(supplied) == Check.BINDING_MISMATCH


async def test_expired_session_is_not_retried_with_renewed_snapshot(supplied):
    expired = replace(
        supplied.session,
        record=replace(supplied.session.record, idle_expires_at_ms=1500),
    )
    references = References(expired, supplied.session)
    assert (
        await evaluate_human_snapshot(
            supplied.binding,
            credential="synthetic-credential",
            sessions=Sessions(supplied.current),
            references=references,
            keys=Keys(supplied.key, supplied.key),
            clock_ms=lambda: 1500,
        )
        == Check.EXPIRED
    )
    assert references.seen == [supplied.binding.session_ref]


async def test_revoked_session_is_not_retried_with_active_snapshot(supplied):
    references = References(PermissionError(), supplied.session)
    assert (
        await evaluate_human_snapshot(
            supplied.binding,
            credential="synthetic-credential",
            sessions=Sessions(supplied.current),
            references=references,
            keys=Keys(supplied.key, supplied.key),
            clock_ms=lambda: 1500,
        )
        == Check.AUTHORITY_REFUSED
    )
    assert references.seen == [supplied.binding.session_ref]


async def test_expiry_between_reads_refuses(supplied):
    times = iter([1500, 2000])
    assert (
        await evaluate_human_snapshot(
            supplied.binding,
            credential="synthetic-credential",
            sessions=Sessions(supplied.current),
            references=References(supplied.session, supplied.session),
            keys=Keys(supplied.key, supplied.key),
            clock_ms=lambda: next(times),
        )
        == Check.EXPIRED
    )


@pytest.mark.parametrize(
    "error,expected",
    [
        (PermissionError(), Check.AUTHORITY_REFUSED),
        (IdentityUnavailable(), Check.AUTHORITY_UNAVAILABLE),
        (TimeoutError(), Check.AUTHORITY_UNAVAILABLE),
    ],
)
async def test_owner_refusal_has_no_fallback(supplied, error, expected):
    keys = Keys(supplied.key)
    assert (
        await evaluate_human_snapshot(
            supplied.binding,
            credential="synthetic-credential",
            sessions=Sessions(supplied.current),
            references=References(error),
            keys=keys,
            clock_ms=lambda: 1500,
        )
        == expected
    )
    assert keys.seen == []


@pytest.mark.parametrize("revision", [True, -1, None, "", 2**64])
def test_revision_is_explicit_global_owner_uint64(supplied, revision):
    with pytest.raises((ValueError, IdentityUnavailable)):
        replace(supplied.binding, authority_revision=revision)


def test_other_key_purpose_is_not_approval(supplied):
    with pytest.raises(ValueError, match="approval-purpose"):
        replace(supplied.key, purpose="webauthn-mfa")


@pytest.mark.spec("ARBORA-A2A-R005.2.4")
@pytest.mark.asyncio
async def test_registration_invalid_proof_never_reaches_native(supplied, monkeypatch):
    from arbora.identity import approval_registration as registration

    async def forbidden(*args, **kwargs):
        pytest.fail("invalid proof reached native registration")

    monkeypatch.setattr(registration, "identity_value", forbidden)
    monkeypatch.setattr(registration.time, "time_ns", lambda: 1000 * 1_000_000)
    binding = registration.ApprovalKeyRegistration(
        "key:one",
        "human:one",
        "tenant:one",
        "signed_approval",
        "ab" * 32,
        0,
        2000,
    )
    with pytest.raises(PermissionError, match="proof refused"):
        await registration.register_approval_key(
            binding,
            "AAAA",
            credential="synthetic-credential",
            sessions=Sessions(supplied.current),
            broker_client=object(),
            graph="identity",
        )


@pytest.mark.spec("ARBORA-A2A-R005.2.4")
@pytest.mark.asyncio
async def test_registration_exact_attestation_after_real_proof(supplied, monkeypatch):
    """Transport contract only; this does not exercise native registration CAS."""
    import base64
    from dataclasses import asdict

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from arbora.identity import approval_registration as registration

    private = Ed25519PrivateKey.generate()
    binding = registration.ApprovalKeyRegistration(
        "key:one",
        "human:one",
        "tenant:one",
        "signed_approval",
        private.public_key().public_bytes_raw().hex(),
        0,
        2000,
    )
    signature = base64.urlsafe_b64encode(private.sign(binding.signing_bytes())).decode()
    expected = RegisteredApprovalKey(
        "key:one",
        "tenant:one",
        "human:one",
        1,
        binding.public_key_hex,
        2000,
        None,
        "signed_approval",
    )
    calls = []

    async def native(client, graph, operation, kind):
        calls.append(operation)
        assert graph == "identity" and kind == "approval_key"
        assert operation == {
            "family": "credential",
            "op": "register_approval_key",
            "request": {
                "session_token": "synthetic-credential",
                "binding": asdict(binding),
                "attestation": "broker_verified_ed25519",
            },
        }
        return asdict(expected)

    monkeypatch.setattr(registration, "identity_value", native)
    monkeypatch.setattr(registration.time, "time_ns", lambda: 1000 * 1_000_000)
    actual = await registration.register_approval_key(
        binding,
        signature,
        credential="synthetic-credential",
        sessions=Sessions(supplied.current),
        broker_client=object(),
        graph="identity",
    )
    assert actual == expected
    assert len(calls) == 1


@pytest.mark.spec("ARBORA-A2A-R005.2.4")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift", [None, "principal", "absolute", "scopes", "revision", "idle"]
)
async def test_renewal_exact_snapshot_and_response_constraints(
    supplied, monkeypatch, drift
):
    """Supplied transport response contract, not native renewal acceptance."""
    from dataclasses import asdict

    from arbora.identity import approval_engine as engine

    before = replace(
        supplied.session,
        record=replace(supplied.session.record, idle_expires_at_ms=2000),
    )
    after = replace(
        before,
        authority_revision=2,
        record=replace(before.record, idle_expires_at_ms=2500),
    )
    if drift == "principal":
        after = replace(after, record=replace(after.record, principal_id="human:two"))
    elif drift == "absolute":
        after = replace(
            after, record=replace(after.record, absolute_expires_at_ms=4000)
        )
    elif drift == "scopes":
        after = replace(after, scopes=("other:read",))
    elif drift == "revision":
        after = replace(after, authority_revision=1)
    elif drift == "idle":
        after = replace(after, record=replace(after.record, idle_expires_at_ms=2000))
    calls = []

    async def native(client, graph, operation, kind):
        calls.append(operation)
        assert operation == {
            "family": "session",
            "op": "renew_reference",
            "session_ref": before.record.session_ref,
            "tenant": before.record.tenant,
            "expected_authority_revision": before.authority_revision,
            "expected_scopes": list(before.scopes),
        }
        return asdict(after)

    monkeypatch.setattr(engine, "identity_value", native)
    resolver = engine.EngineOriginalSessionResolver(
        object(), graph="identity", tenant="tenant:one"
    )
    if drift is None:
        assert await resolver.renew_original_session(before) == after
    else:
        with pytest.raises(PermissionError):
            await resolver.renew_original_session(before)
    assert len(calls) == 1
    assert supplied.binding.authority_revision == 1
    assert supplied.binding.approval_expires_at_ms == 2000
