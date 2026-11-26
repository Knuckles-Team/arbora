"""Admit an explicit completed-child continuation (ARBORA-RELEASE-R003.4.13)."""

from __future__ import annotations

import pytest

from arbora.a2a.authority import A2AIdempotencyConflict
from arbora.api.invoke.pipeline import OperationRefused
from tests.api._child_admission_support import AdmissionEngine, authority


def _args(**over: object) -> dict[str, object]:
    base = {
        "conversation_target": "done:1",
        "history_revision": 5,
        "content": "now summarise",
        "idempotency_key": "c1",
    }
    return {**base, **over}


@pytest.mark.spec("ARBORA-RELEASE-R003.4.13")
@pytest.mark.asyncio
async def test_retry_starts_one_continuation(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    first = await auth.admit_continuation(**_args())  # type: ignore[arg-type]
    again = await auth.admit_continuation(**_args())  # type: ignore[arg-type]
    assert first["run_id"] == again["run_id"] and first["created"] is True
    assert again["created"] is False and engine.delivered == 1
    request = next(iter(engine.rows.values()))[1]
    assert request["conversation"] == {
        "context_id": "done:1",
        "expected_revision": 5,
        "predecessor_run_id": "done:1",
        "schema_version": "1",
    }
    assert first["predecessor_run_id"] == "done:1"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.13")
@pytest.mark.asyncio
async def test_changed_history_or_content_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    await auth.admit_continuation(**_args())  # type: ignore[arg-type]
    with pytest.raises(A2AIdempotencyConflict):
        await auth.admit_continuation(**_args(content="other"))  # type: ignore[arg-type]
    with pytest.raises(OperationRefused) as stale:
        await auth.admit_continuation(  # type: ignore[arg-type]
            **_args(idempotency_key="c2", history_revision=4)
        )
    assert stale.value.code == "HISTORY_REVISION_CONFLICT"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.13")
@pytest.mark.asyncio
async def test_foreign_incomplete_and_unsupported_histories_start_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    with pytest.raises(OperationRefused) as active:
        await auth.admit_continuation(  # type: ignore[arg-type]
            **_args(conversation_target="child:1", history_revision=3)
        )
    assert active.value.code == "CONTINUATION_LIFECYCLE_INVALID"
    foreign = authority(monkeypatch, engine, owner="intruder")
    with pytest.raises(OperationRefused) as owner:
        await foreign.admit_continuation(**_args())  # type: ignore[arg-type]
    assert owner.value.code == "CHILD_OWNER_MISMATCH"
    assert engine.delivered == 0
