"""Admit a message to an active child (ARBORA-RELEASE-R003.4.12)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.a2a.authority import A2AIdempotencyConflict
from arbora.api.invoke.pipeline import OperationRefused
from tests.api._child_admission_support import (
    DONE,
    AdmissionEngine,
    authority,
    install_engine,
)
from tests.api._served_support import McpAsk, capture_invocation, person
from tests.test_served_operation_api import _call, _value, served  # noqa: F401


def _args(**over: Any) -> dict[str, Any]:
    base = {
        "conversation_target": "child:1",
        "expected_revision": 3,
        "content": "please also check X",
        "idempotency_key": "k1",
    }
    return {**base, **over}


@pytest.mark.spec("ARBORA-RELEASE-R003.4.12")
@pytest.mark.asyncio
async def test_retry_delivers_at_most_once(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    first = await auth.admit_child_message(**_args())
    again = await auth.admit_child_message(**_args())
    assert first["state"] == "queued" and first["created"] is True
    assert again["created"] is False and again["work_item_id"] == first["work_item_id"]
    assert engine.delivered == 1
    with pytest.raises(A2AIdempotencyConflict):
        await auth.admit_child_message(**_args(content="different"))
    row = engine.rows["k1"][1]
    assert row["child_input"]["state"] == "queued"
    assert row["child_input"]["child_run_id"] == "child:1"


@pytest.mark.spec("ARBORA-RELEASE-R003.4.12")
@pytest.mark.asyncio
async def test_wrong_owner_stale_revision_and_oversize_refuse_before_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    for over, code in (
        ({"expected_revision": 2}, "CHILD_REVISION_STALE"),
        ({"content": "x" * 16_385}, "CHILD_INPUT_TOO_LARGE"),
        (
            {"conversation_target": DONE, "expected_revision": 5},
            "CHILD_TARGET_TERMINAL",
        ),
    ):
        with pytest.raises(OperationRefused) as refused:
            await auth.admit_child_message(**_args(**over))
        assert refused.value.code == code
    engine.owners["child:1"] = "someone-else"
    with pytest.raises(OperationRefused) as owner:
        await auth.admit_child_message(**_args())
    assert owner.value.code == "CHILD_OWNER_MISMATCH"
    assert engine.delivered == 0


@pytest.mark.spec("ARBORA-RELEASE-R003.4.12")
@pytest.mark.asyncio
async def test_completion_race_gives_one_explicit_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = AdmissionEngine()
    auth = authority(monkeypatch, engine)
    engine.lifecycle["child:1"] = ("completed", 4)
    with pytest.raises(OperationRefused) as refused:
        await auth.admit_child_message(**_args(expected_revision=4))
    assert refused.value.code == "CHILD_TARGET_TERMINAL"
    assert engine.delivered == 0


@pytest.mark.spec("ARBORA-RELEASE-R003.4.12")
def test_trace_step_is_not_a_child_target_and_pinned_fields_are_guarded() -> None:
    from arbora.api.ops.agents import ChildMessageParams, operations

    ids = {op.id for op in operations()}
    assert {"agents.tasks.message", "agents.tasks.continue"} <= ids
    with pytest.raises(ValueError):
        ChildMessageParams(conversation_target="", expected_revision=0, content="x")
    from arbora.pin_lag.meristem import require_submit_field

    require_submit_field("child_input")
    require_submit_field("conversation")


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Any:
    return capture_invocation(monkeypatch)


@pytest.mark.spec("ARBORA-RELEASE-R003.4.12")
def test_message_is_served_over_http_and_mcp(
    captured: Any,
    served: Any,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _client = served
    engine = AdmissionEngine()
    install_engine(monkeypatch, engine)
    key = {"Idempotency-Key": "http-1"}
    body = {"conversation_target": "child:1", "expected_revision": 3, "content": "hi"}
    first = _value(_call(app, "agents.tasks.message", body, **key))
    again = _value(_call(app, "agents.tasks.message", body, **key))
    assert first["state"] == "queued" and first["created"] is True
    assert again["created"] is False and engine.delivered == 1
    conflict = _call(app, "agents.tasks.message", {**body, "content": "other"}, **key)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    stale = _call(
        app,
        "agents.tasks.message",
        {**body, "expected_revision": 1},
        **{"Idempotency-Key": "http-2"},
    )
    assert stale.json()["error"]["code"] == "CHILD_REVISION_STALE"
    assert _call(app, "agents.tasks.message", body).status_code == 400
    ask = McpAsk(captured["invocation"], monkeypatch, person("one"))
    answer = ask(
        "act",
        "agents.tasks.message",
        {**body, "content": "via mcp"},
        execute=True,
        idempotency_key="mcp-1",
    )
    assert answer["ok"] is True, answer
    assert answer["result"]["value"]["state"] == "queued"
    assert engine.delivered == 2
