"""An anonymous effect needs an operator policy entry (ARBORA-IDENTITY-R004.6)."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import _anonymous_effect
from arbora.api.invoke.steps import AnonymousCaller, VerifiedCaller
from arbora.api.registry import Effect
from arbora.core.claims import params_digest
from arbora.security import anonymous


def _caller(kind):
    return kind(
        principal="anonymous",
        tenant="local",
        effective_scopes=frozenset(),
        engine_claims={},
        principal_kind="anonymous",
        authenticated=False,
        delegated=False,
        credential_kind="none",
        policy_revision="p",
    )


class _Op:
    id = "memory.remember"
    effect = Effect.WRITE


PARAMS = {"target": "note:1", "text": "x"}


@pytest.mark.spec("ARBORA-IDENTITY-R004.6")
def test_effect_needs_operator_policy_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    anon = _caller(AnonymousCaller)
    # The default is an empty list: every effect is denied.
    monkeypatch.setattr(anonymous, "effect_policy", lambda document=None: ())
    refused = _anonymous_effect(_Op(), PARAMS, anon)
    assert refused is not None and refused.code == "ANONYMOUS_EFFECT_NOT_ADMITTED"
    digest = params_digest(PARAMS)
    entry = ("memory.remember", "note:1", digest)
    monkeypatch.setattr(anonymous, "effect_policy", lambda document=None: (entry,))
    assert _anonymous_effect(_Op(), PARAMS, anon) is None  # an admitted effect
    changed = {**PARAMS, "text": "y"}  # a digest mismatch is not admitted
    assert _anonymous_effect(_Op(), changed, anon) is not None
    assert _anonymous_effect(_Op(), {**PARAMS, "target": "note:2"}, anon) is not None
    # A verified caller and a read never consult the list.
    assert _anonymous_effect(_Op(), PARAMS, _caller(VerifiedCaller)) is None
    read = type("R", (), {"id": "x", "effect": Effect.READ})()
    assert _anonymous_effect(read, PARAMS, anon) is None


@pytest.mark.spec("ARBORA-IDENTITY-R004.6")
def test_policy_document_drops_malformed_entries() -> None:
    good = "a" * 64
    document = {
        "identity": {
            "anonymous_effect_policy": [
                {"op_id": "o", "target": "t", "action_digest": good},
                {"op_id": "o", "target": "t", "action_digest": "ABC"},
                {"op_id": "o"},
                "nope",
            ]
        }
    }
    assert anonymous.effect_policy(document) == (("o", "t", good),)
    assert anonymous.effect_policy({}) == ()
