"""Every write, act or admin operation names the owner that settles its replay.

The served pipeline refuses a governed operation with no replay owner
("durable effect owner unavailable"), so an operation that declares none is
unservable. The owner is a declared ``NATIVE_IDEMPOTENCY`` entry (the engine
replays by the key), the effect journal's operation list, or a plan gate.
"""

from __future__ import annotations

import pytest

from arbora.api.invoke import pipeline
from arbora.api.invoke.pipeline import uncovered_write_operations
from arbora.api.ops.registry_factory import get_registry

pytestmark = pytest.mark.spec("ARBORA-OPS-R009.10")


def test_every_write_operation_declares_a_replay_owner() -> None:
    offenders = uncovered_write_operations(get_registry())
    assert not offenders, (
        f"{len(offenders)} write, act or admin operation(s) declare no replay "
        "owner; add the id to its module's NATIVE_IDEMPOTENCY (the engine "
        "replays by key) or to EXTERNAL_EFFECT_OPERATIONS: " + ", ".join(offenders)
    )


def test_the_check_flags_an_operation_that_loses_its_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = get_registry()
    assert "rlm.run" not in uncovered_write_operations(registry)
    monkeypatch.setattr(
        pipeline,
        "EXTERNAL_EFFECT_OPERATIONS",
        tuple(op for op in pipeline.EXTERNAL_EFFECT_OPERATIONS if op != "rlm.run"),
    )
    assert "rlm.run" in uncovered_write_operations(registry)
