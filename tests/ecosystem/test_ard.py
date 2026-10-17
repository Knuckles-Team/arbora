"""ARD publication: signed entries and federated result dedupe."""

from __future__ import annotations

import pytest

from graph_os.ecosystem import ard_federation, ard_signing


@pytest.fixture(autouse=True)
def _fixed_key(monkeypatch):
    monkeypatch.setenv("ARD_SIGNING_PRIVATE_KEY", "a-test-key-that-is-not-a-secret")


def test_a_signed_entry_verifies_and_a_changed_entry_does_not():
    if not ard_signing.signing_available():
        pytest.skip("the signing library is absent")
    entry = {"id": "server:one", "name": "One"}
    signature = ard_signing.sign_datapoint(entry)
    key = ard_signing.public_key_b64()
    assert signature and key
    assert ard_signing.verify_datapoint(entry, signature, key)
    assert not ard_signing.verify_datapoint({**entry, "name": "Two"}, signature, key)


def test_canonical_form_ignores_key_order():
    assert ard_signing.canonical({"b": 1, "a": 2}) == ard_signing.canonical(
        {"a": 2, "b": 1}
    )


def test_dedupe_keeps_the_best_score_per_publisher_and_id():
    results = [
        {"id": "x", "score": 0.2, "publisher": {"domain": "a"}},
        {"id": "x", "score": 0.9, "publisher": {"domain": "a"}},
        {"id": "x", "score": 0.5, "publisher": {"domain": "b"}},
    ]
    kept = ard_federation._dedupe(results)
    assert sorted((r["publisher"]["domain"], r["score"]) for r in kept) == [
        ("a", 0.9),
        ("b", 0.5),
    ]
