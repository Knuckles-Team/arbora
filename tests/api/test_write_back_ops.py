"""ARBORA-FLEET-R003.2: write-back is a typed, gated registry operation."""

from __future__ import annotations

import pytest

from arbora.api.invoke.pipeline import EXTERNAL_EFFECT_OPERATIONS
from arbora.api.ops import get_registry
from arbora.api.registry import Confirm, Effect, Idempotency


@pytest.mark.spec("ARBORA-FLEET-R003.2")
def test_preview_commit_replay_and_refusals() -> None:
    registry = get_registry()
    preview = registry.get("fleet.write_back.preview")
    commit = registry.get("fleet.write_back.commit")
    assert preview is not None and preview.effect is Effect.READ
    assert commit is not None
    assert commit.effect is Effect.WRITE
    assert commit.confirm is Confirm.PLAN
    assert commit.idempotency is Idempotency.KEY_REQUIRED
    # The effect journal is the replay owner of a commit, so a replayed commit
    # returns the original result and a missing plan or key refuses.
    assert "fleet.write_back.commit" in EXTERNAL_EFFECT_OPERATIONS
    assert registry.get("fleet.write_back.apply") is None  # no native child path
