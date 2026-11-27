"""Admit an explicit completed-child continuation (ARBORA-RELEASE-R003.4.13)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from arbora.api.invoke.pipeline import OperationRefused
from arbora.core.a2a_errors import A2AIdempotencyConflict
from tests.api._child_admission_support import (
    DONE,
    LIVE,
    AdmissionEngine,
    authority,
    install_engine,
)
from tests.api._served_support import McpAsk, capture_invocation, person
from tests.test_served_operation_api import _call, _value, served  # noqa: F401


def _args(**over: Any) -> dict[str, Any]:
    base = {
        "conversation_target": DONE,
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
    first = await auth.admit_continuation(**_args())
    again = await auth.admit_continuation(**_args())
    assert first["run_id"] == again["run_id"] and first["created"] is True
    assert again["created"] is False and engine.delivered == 1
    request = next(iter(engine.rows.values()))[1]
    assert request["conversation"] == {
        "context_id": DONE,
        "expected_revision": 5,
        "predecessor_run_id": DONE,
        "schema_version": "1",
    }
    assert first["predecessor_run_id"] == DONE
    # One run starts, on the agent the predecessor chose; the replay starts none.
    await asyncio.sleep(0)
    assert [run["agent_name"] for run in engine.runs] == ["helper"]
    assert engine.runs[0]["run_id"] == first["run_id"]
    assert first["agent_name"] == "helper"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.13")
@pytest.mark.asyncio
async def test_changed_history_or_content_conflicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    await auth.admit_continuation(**_args())
    with pytest.raises(A2AIdempotencyConflict):
        await auth.admit_continuation(**_args(content="other"))
    with pytest.raises(OperationRefused) as stale:
        await auth.admit_continuation(**_args(idempotency_key="c2", history_revision=4))
    assert stale.value.code == "HISTORY_REVISION_CONFLICT"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.13")
@pytest.mark.asyncio
async def test_foreign_incomplete_and_unsupported_histories_start_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    with pytest.raises(OperationRefused) as active:
        await auth.admit_continuation(
            **_args(conversation_target=LIVE, history_revision=3)
        )
    assert active.value.code == "CONTINUATION_LIFECYCLE_INVALID"
    engine.owners[DONE] = "someone-else"
    with pytest.raises(OperationRefused) as owner:
        await auth.admit_continuation(**_args())
    assert owner.value.code == "CHILD_OWNER_MISMATCH"
    assert engine.delivered == 0 and engine.runs == []


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


@pytest.mark.spec("ARBORA-RELEASE-R003.4.13")
def test_continuation_is_served_over_http_and_mcp(
    captured: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client = served
    engine = AdmissionEngine()
    engine.agent = "researcher"
    install_engine(monkeypatch, engine)
    body = {"conversation_target": DONE, "history_revision": 5, "content": "go on"}
    key = {"Idempotency-Key": "http-c1"}
    first = _value(_call(app, "agents.tasks.continue", body, **key))
    again = _value(_call(app, "agents.tasks.continue", body, **key))
    assert first["run_id"] == again["run_id"] and again["created"] is False
    assert first["agent_name"] == "researcher" and engine.delivered == 1
    live = _call(
        app,
        "agents.tasks.continue",
        {**body, "conversation_target": LIVE, "history_revision": 3},
        **{"Idempotency-Key": "http-c2"},
    )
    assert live.json()["error"]["code"] == "CONTINUATION_LIFECYCLE_INVALID"
    ask = McpAsk(captured["invocation"], monkeypatch, person("one"))
    answer = ask(
        "act",
        "agents.tasks.continue",
        {**body, "content": "again"},
        execute=True,
        idempotency_key="mcp-c1",
    )
    assert answer["ok"] is True, answer
    assert answer["result"]["value"]["predecessor_run_id"] == DONE
    assert engine.delivered == 2
