"""Run tokens are signed, expire, and enforce their allowlists."""

from __future__ import annotations

import pytest

from arbora.security.run_token import TokenError, mint_token, validate_token


@pytest.fixture(autouse=True)
def _secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARBORA_TOKEN_SECRET", "s" * 48)


def test_round_trip_keeps_scope() -> None:
    token = mint_token("run-1", endpoints=("graph",), operations=("read",), now=100.0)
    decoded = validate_token(token, endpoint="graph", operation="read", now=101.0)
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
def test_refusals(mutate, kwargs) -> None:
    token = mint_token(
        "run-1", endpoints=("graph",), operations=("read",), ttl_seconds=60, now=100.0
    )
    with pytest.raises(TokenError):
        validate_token(mutate(token), **kwargs)


def test_retired_secret_names_are_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbora.security.run_token import _secret as _resolve_secret  # noqa: PLC0415

    monkeypatch.delenv("ARBORA_TOKEN_SECRET", raising=False)
    monkeypatch.setenv("AGENT_UTILITIES_TOKEN_SECRET", "old" * 16)
    assert b"old" * 16 != _resolve_secret()
