"""The process credential lease is one explicit state machine (ARBORA-IDENTITY-R027.1, .4, .7)."""

from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest

from arbora.security.actor_context import (
    LEASE_EDGES,
    LEASE_TRANSITION_ILLEGAL,
    TERMINAL_LEASE_STATES,
    CredentialExpiredError,
    CredentialLease,
    IllegalLeaseTransition,
    LeaseEdge,
    LeaseEvent,
    LeaseGuard,
    LeaseState,
)

ISSUED = 1_800_000_000.0
TTL = 300
EXPIRES = int(ISSUED) + TTL
HALF = ISSUED + TTL / 2
NEW_EXPIRY = EXPIRES + 1_000

#: The moment at which each guard holds (and the one it needs to fail).
_GUARD_MOMENT = {
    None: ISSUED + 10,
    LeaseGuard.AT_HALF_LIFETIME: HALF + 1,
    LeaseGuard.BEFORE_EXPIRY: HALF + 1,
    LeaseGuard.AT_EXPIRY: EXPIRES + 1,
    LeaseGuard.IDENTITY_UNCHANGED: EXPIRES + 1,
}

#: How each source state is reached through legal events only.
_PATH: dict[LeaseState, tuple[tuple[LeaseEvent, float], ...]] = {
    LeaseState.LIVE: (),
    LeaseState.RENEW_DUE: ((LeaseEvent.TICK, HALF),),
    LeaseState.RENEWING: ((LeaseEvent.TICK, HALF), (LeaseEvent.RENEW_START, HALF)),
    LeaseState.DEGRADED: (
        (LeaseEvent.TICK, HALF),
        (LeaseEvent.RENEW_START, HALF),
        (LeaseEvent.IDP_ERR, HALF),
    ),
    LeaseState.EXPIRED: ((LeaseEvent.TICK, EXPIRES),),
    LeaseState.REVOKED: ((LeaseEvent.REVOKE, ISSUED),),
    LeaseState.STOPPED: ((LeaseEvent.STOP, ISSUED),),
}


class Recording(CredentialLease):
    """Counts how many times ``state`` is assigned."""

    writes = 0

    def __setattr__(self, name: str, value: object) -> None:
        if name == "state":
            type(self).writes += 1
        super().__setattr__(name, value)


def _lease_in(state: LeaseState, cls: type[CredentialLease] = CredentialLease):
    lease = cls(EXPIRES, issued_at=ISSUED)
    for event, now in _PATH[state]:
        _send(lease, event, now)
    assert lease.state is state
    return lease


def _send(
    lease: CredentialLease,
    event: LeaseEvent,
    now: float,
    *,
    identity_unchanged: bool = True,
) -> LeaseState:
    if event is LeaseEvent.IDP_OK:
        return lease.apply(
            event, now, identity_unchanged=identity_unchanged, expires_at=NEW_EXPIRY
        )
    code = {
        LeaseEvent.IDP_ERR: "acquisition_failed",
        LeaseEvent.IDENTITY_CHANGED: "roles_revoked",
        LeaseEvent.REVOKE: "identity_changed",
    }.get(event)
    return lease.apply(event, now, identity_unchanged=identity_unchanged, code=code)


def _snapshot(lease: CredentialLease) -> tuple[object, ...]:
    return (
        lease.state,
        lease.expires_at,
        lease.attempts,
        lease.failure_code,
        lease.revocation_reason,
        lease.renew_due_at,
    )


def _edge_moment(edge: LeaseEdge) -> float:
    if edge.source is LeaseState.EXPIRED and edge.guard is None:
        return EXPIRES + 1
    if edge.source is LeaseState.DEGRADED and edge.guard is None:
        return HALF + 2
    return _GUARD_MOMENT[edge.guard]


@pytest.mark.spec("ARBORA-IDENTITY-R027.1")
def test_the_table_has_the_twenty_six_specified_edges() -> None:
    assert len(LEASE_EDGES) == 26
    assert len({edge.edge_id for edge in LEASE_EDGES}) == 26
    assert TERMINAL_LEASE_STATES == {LeaseState.REVOKED, LeaseState.STOPPED}
    assert not any(edge.source in TERMINAL_LEASE_STATES for edge in LEASE_EDGES)


@pytest.mark.spec("ARBORA-IDENTITY-R027.1")
@pytest.mark.parametrize("edge", LEASE_EDGES, ids=lambda edge: edge.edge_id)
def test_legal_edge(edge: LeaseEdge) -> None:
    lease = _lease_in(edge.source, Recording)
    attempts_before = lease.attempts
    Recording.writes = 0
    assert _send(lease, edge.event, _edge_moment(edge)) is edge.target
    assert lease.state is edge.target
    assert Recording.writes == 1
    # The data each target carries.
    if edge.target is LeaseState.LIVE:
        assert (lease.expires_at, lease.attempts, lease.failure_code) == (
            NEW_EXPIRY,
            0,
            None,
        )
    elif edge.event is LeaseEvent.IDP_ERR:
        assert lease.attempts == attempts_before + 1
        assert lease.failure_code == "acquisition_failed"
        assert lease.expires_at == EXPIRES
    elif edge.target is LeaseState.REVOKED:
        expected = (
            "roles_revoked"
            if edge.event is LeaseEvent.IDENTITY_CHANGED
            else "identity_changed"
        )
        assert lease.revocation_reason == expected
    else:
        assert lease.expires_at == EXPIRES
        assert lease.revocation_reason is None


def _illegal_pairs() -> list[tuple[LeaseState, LeaseEvent]]:
    legal = {(edge.source, edge.event) for edge in LEASE_EDGES}
    return [
        pair for pair in itertools.product(LeaseState, LeaseEvent) if pair not in legal
    ]


#: Legal pairs whose every guard fails: ``(state, event, now, identity_unchanged)``.
_FAILED_GUARDS = {
    "live-tick-early": (LeaseState.LIVE, LeaseEvent.TICK, HALF - 1, True),
    "renewdue-tick-early": (LeaseState.RENEW_DUE, LeaseEvent.TICK, HALF + 1, True),
    "renewing-tick-early": (LeaseState.RENEWING, LeaseEvent.TICK, HALF + 1, True),
    "degraded-tick-early": (LeaseState.DEGRADED, LeaseEvent.TICK, HALF + 1, True),
    "renewing-idp_ok-identity_changed": (
        LeaseState.RENEWING,
        LeaseEvent.IDP_OK,
        HALF + 1,
        False,
    ),
    "degraded-idp_ok-identity_changed": (
        LeaseState.DEGRADED,
        LeaseEvent.IDP_OK,
        HALF + 1,
        False,
    ),
    "expired-idp_ok-identity_changed": (
        LeaseState.EXPIRED,
        LeaseEvent.IDP_OK,
        EXPIRES + 1,
        False,
    ),
}


@pytest.mark.spec("ARBORA-IDENTITY-R027.4")
def test_every_illegal_pair_is_refused_and_state_is_unchanged() -> None:
    pairs = _illegal_pairs()
    # 7 states x 7 events = 49 pairs; 24 distinct pairs carry the 26 edges.
    assert len(pairs) == 49 - 24
    cases = [(state, event, ISSUED + 10, True) for state, event in pairs]
    cases += list(_FAILED_GUARDS.values())
    for state, event, now, unchanged in cases:
        lease = _lease_in(state)
        before = _snapshot(lease)
        with pytest.raises(IllegalLeaseTransition) as refused:
            _send(lease, event, now, identity_unchanged=unchanged)
        error = refused.value
        assert (error.code, error.machine, error.from_state, error.event) == (
            LEASE_TRANSITION_ILLEGAL,
            "credential_lease",
            state,
            event,
        )
        assert isinstance(error, CredentialExpiredError)  # still fails closed
        assert _snapshot(lease) == before, (state, event)

    # The spec's examples, and every event after Stopped.
    for event in LeaseEvent:
        with pytest.raises(IllegalLeaseTransition):
            _send(_lease_in(LeaseState.STOPPED), event, ISSUED + 10)
    with pytest.raises(IllegalLeaseTransition):
        _send(_lease_in(LeaseState.REVOKED), LeaseEvent.IDP_OK, ISSUED + 10)


@pytest.mark.spec("ARBORA-IDENTITY-R027.4")
def test_renewing_idp_err_late_takes_the_expiry_edge_not_degraded() -> None:
    """``renewing-idp_err-late``: ``before_expiry`` fails, so ``at_expiry`` decides."""
    lease = _lease_in(LeaseState.RENEWING)
    assert _send(lease, LeaseEvent.IDP_ERR, EXPIRES) is LeaseState.EXPIRED


@pytest.mark.spec("ARBORA-IDENTITY-R027.3")
def test_renew_recovers_degraded_and_expired_but_never_a_terminal_lease() -> None:
    for state in (LeaseState.DEGRADED, LeaseState.EXPIRED):
        lease = _lease_in(state)
        lease.renew(NEW_EXPIRY, now=EXPIRES + 5)
        assert (lease.state, lease.expires_at) == (LeaseState.LIVE, NEW_EXPIRY)
    for state in TERMINAL_LEASE_STATES:
        lease = _lease_in(state)
        with pytest.raises(CredentialExpiredError):
            lease.renew(NEW_EXPIRY, now=ISSUED + 10)
        assert lease.state is state


@pytest.mark.spec("ARBORA-IDENTITY-R027.3")
def test_request_checks_fail_closed_while_expired_or_revoked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    monkeypatch.setattr(time, "time", lambda: EXPIRES + 1.0)
    for state in (LeaseState.LIVE, LeaseState.EXPIRED, LeaseState.REVOKED):
        lease = _lease_in(state)
        with pytest.raises(CredentialExpiredError):
            lease.ensure_current()
    # A Degraded lease stays valid until its expiry time.
    monkeypatch.setattr(time, "time", lambda: HALF + 5)
    _lease_in(LeaseState.DEGRADED).ensure_current()


_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.spec("ARBORA-IDENTITY-R027.7")
def test_only_apply_writes_state() -> None:
    """Source search: nothing outside ``CredentialLease.apply`` assigns ``state``."""
    module = ast.parse((_REPO / "arbora/security/actor_context.py").read_text())
    lease_class = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "CredentialLease"
    )
    writers = {
        method.name
        for method in lease_class.body
        if isinstance(method, ast.FunctionDef)
        for node in ast.walk(method)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign))
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Attribute) and target.attr == "state"
    }
    assert writers == {"__init__", "apply"}

    offenders: list[str] = []
    for path in sorted((_REPO / "arbora").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and target.attr == "state"
                    and "lease" in ast.unparse(target.value).lower()
                ):
                    offenders.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    assert offenders == []
