"""Run tokens are signed, expire, and enforce their allowlists."""

from __future__ import annotations

import pytest

from arbora.security.run_token import (
    RunTokenSecretUnavailable,
    RunTokenSigner,
    TokenError,
    token_secret_from_config,
)


@pytest.fixture
def signer() -> RunTokenSigner:
    return RunTokenSigner(lambda: "s" * 48)


def test_round_trip_keeps_scope(signer: RunTokenSigner) -> None:
    token = signer.mint("run-1", endpoints=("graph",), operations=("read",), now=100.0)
    decoded = signer.validate(token, endpoint="graph", operation="read", now=101.0)
    assert decoded.run_id == "run-1"


@pytest.mark.parametrize(
    ("mutate", "kwargs"),
    [
        (lambda t: t, {"now": 10_000.0}),
        (lambda t: t, {"endpoint": "other", "now": 101.0}),
        (lambda t: t, {"operation": "write", "now": 101.0}),
        (lambda t: t[:-2] + ("AA" if not t.endswith("AA") else "BB"), {"now": 101.0}),
    ],
    ids=["expired", "endpoint", "operation", "tampered"],
)
def test_refusals(signer: RunTokenSigner, mutate, kwargs) -> None:
    token = signer.mint(
        "run-1", endpoints=("graph",), operations=("read",), ttl_seconds=60, now=100.0
    )
    with pytest.raises(TokenError):
        signer.validate(mutate(token), **kwargs)


def test_retired_secret_names_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARBORA_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("MERISTEM_TOKEN_SECRET", "old" * 16)
    assert token_secret_from_config() is None
    with pytest.raises(RunTokenSecretUnavailable):
        RunTokenSigner(token_secret_from_config).mint("run-1")


def test_two_signers_with_different_secrets_refuse_each_other() -> None:
    first, second = RunTokenSigner(lambda: "a" * 48), RunTokenSigner(lambda: "b" * 48)
    token = first.mint("run-1", now=100.0)
    assert first.validate(token, now=101.0).run_id == "run-1"
    with pytest.raises(TokenError):
        second.validate(token, now=101.0)
