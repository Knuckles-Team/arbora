"""The evolution operations forward to the engine as the caller (ARBORA-OPS-R022.2.1 to R022.2.5)."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import medulla.generated as generated
import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _engine, evolution
from arbora.api.registry import Effect, Registry

OPS = {op.id: op for op in evolution.operations()}


class _Client:
    """A structural double for a medulla client; answers by engine method."""

    authenticated = True

    def __init__(self, answers: dict[str, Any], refuse: str | None = None) -> None:
        self.sent: list[tuple[str, Any, str | None]] = []
        self.keys: list[Any] = []
        self.claims: list[dict[str, Any]] = []
        self._answers = answers
        self._refuse = refuse

    def use_verified_context(self, claims: Any) -> Any:
        self.claims.append(dict(claims))
        return contextlib.nullcontext()

    async def _send(self, method: str, params: Any, graph: str, **keywords: Any) -> Any:
        self.sent.append((method, params, graph))
        self.keys.append(keywords.get("idempotency_key"))
        if self._refuse is not None:
            from medulla import EngineResponseError

            raise EngineResponseError(self._refuse, "refused")
        return self._answers[method]


def _context(client: _Client, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def graph(_context: Any) -> str:
        return "tenant-content"

    monkeypatch.setattr(_engine, "caller_dispatch_graph", graph)
    monkeypatch.setattr(evolution, "caller_dispatch_graph", graph)
    caller = SimpleNamespace(
        tenant="tenant-a", principal="alice", engine_claims={"principal": "alice"}
    )
    return SimpleNamespace(
        caller=caller, client=client, idempotency_key="key-1", services={}
    )


def _serve(monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
    from medulla.generated import _runtime

    declared = frozenset(generated.METHOD_IDS) | {"AnalyticsJob", *extra}
    monkeypatch.setattr(generated, "METHOD_IDS", declared)
    monkeypatch.setattr(_runtime, "METHOD_IDS", declared)


def test_operations_declare_scopes_effects_and_keys() -> None:
    Registry(tuple(OPS.values()))
    for op_id in (
        "evolution.loops.status",
        "evolution.schedules.list",
        "evolution.proposals.list",
    ):
        assert OPS[op_id].scopes == {"loops:read"}
        assert OPS[op_id].effect is Effect.READ
    for op_id in ("evolution.loops.run", "evolution.loops.pause"):
        assert OPS[op_id].scopes == {"loops:control"}
        assert OPS[op_id].effect is Effect.WRITE
        assert OPS[op_id].idempotency.name == "KEY_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R022.2.1")
@pytest.mark.asyncio
async def test_status_reaches_runner_with_tenant_and_declares_loops_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "LoopStatus")
    reply = {"state": "running", "last_run_ms": 5, "next_run_ms": 9}
    client = _Client({"LoopStatus": {**reply, "internal": "dropped"}})
    answer = await evolution.handle_loops_status(
        _context(client, monkeypatch), {"loop_id": "L1"}, OPS["evolution.loops.status"]
    )
    assert answer == {"value": reply}
    assert client.sent == [("LoopStatus", {"loop_id": "L1"}, "tenant-content")]
    assert client.claims == [{"principal": "alice"}]
    assert OPS["evolution.loops.status"].scopes == {"loops:read"}


@pytest.mark.spec("ARBORA-OPS-R022.2.1")
@pytest.mark.asyncio
async def test_status_unknown_loop_is_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "LoopStatus")
    context = _context(_Client({}, refuse="NOT_FOUND"), monkeypatch)
    with pytest.raises(EngineRefusal) as refused:
        await evolution.handle_loops_status(
            context, {"loop_id": "x"}, OPS["evolution.loops.status"]
        )
    assert refused.value.code == "NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R022.2.1")
@pytest.mark.spec("ARBORA-OPS-R022.2.4")
@pytest.mark.spec("ARBORA-OPS-R022.2.5")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "handler", "method"),
    [
        ("evolution.loops.status", evolution.handle_loops_status, "LoopStatus"),
        ("evolution.loops.run", evolution.handle_loops_run, "LoopRun"),
        ("evolution.loops.pause", evolution.handle_loops_pause, "LoopPause"),
    ],
)
async def test_pinned_engine_refuses_naming_the_missing_method(
    monkeypatch: pytest.MonkeyPatch, op_id: str, handler: Any, method: str
) -> None:
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {method}
    )
    client = _Client({})
    with pytest.raises(OperationRefused) as refused:
        await handler(_context(client, monkeypatch), {"loop_id": "L1"}, OPS[op_id])
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == method
    assert client.sent == []


@pytest.mark.spec("ARBORA-OPS-R022.2.2")
@pytest.mark.asyncio
async def test_schedules_list_reaches_runner_with_tenant_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    rows = [
        {"name": "b", "next_run_ms": 20},
        {"name": "a", "next_run_ms": 20},
        {"name": "z", "next_run_ms": 10},
    ]
    client = _Client({"AnalyticsJob": {"schedules": rows}})
    context = _context(client, monkeypatch)
    op = OPS["evolution.schedules.list"]
    first = await evolution.handle_schedules_list(context, {"limit": 2}, op)
    assert [row["name"] for row in first["value"]["items"]] == ["z", "a"]
    assert client.sent[0][1] == {"op": {"ScheduleList": {"graph": "tenant-content"}}}
    cursor = first["value"]["next_cursor"]
    second = await evolution.handle_schedules_list(
        context, {"limit": 2, "cursor": cursor}, op
    )
    assert [row["name"] for row in second["value"]["items"]] == ["b"]
    assert second["value"]["next_cursor"] is None
    with pytest.raises(OperationRefused) as bad:
        await evolution.handle_schedules_list(context, {"limit": 2, "cursor": "!!"}, op)
    assert bad.value.code == "INVALID_ARGUMENT"
    broken = _context(_Client({"AnalyticsJob": "nope"}), monkeypatch)
    with pytest.raises(OperationRefused) as closed:
        await evolution.handle_schedules_list(broken, {"limit": 2}, op)
    assert closed.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R022.2.3")
@pytest.mark.asyncio
async def test_proposals_list_reaches_runner_with_tenant_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "RevisionProposalList")
    page = {
        "rows": [
            {"id": "p1", "stage": "proposed", "digest": "d1", "subject_ref": "s"},
        ],
        "next_cursor": "c2",
        "schema_version": 1,
    }
    client = _Client({"RevisionProposalList": page})
    context = _context(client, monkeypatch)
    answer = await evolution.handle_proposals_list(
        context, {"limit": 10, "cursor": "c1"}, OPS["evolution.proposals.list"]
    )
    assert answer == {
        "value": {
            "items": [{"id": "p1", "state": "proposed", "digest": "d1"}],
            "next_cursor": "c2",
        }
    }
    assert client.sent[0][1] == {"request": {"limit": 10, "cursor": "c1"}}
    refusing = _context(_Client({}, refuse="INVALID_ARGUMENT"), monkeypatch)
    with pytest.raises(EngineRefusal) as refused:
        await evolution.handle_proposals_list(
            refusing, {"limit": 10, "cursor": "bad"}, OPS["evolution.proposals.list"]
        )
    assert refused.value.code == "INVALID_ARGUMENT"


@pytest.mark.spec("ARBORA-OPS-R022.2.4")
@pytest.mark.asyncio
async def test_run_requires_loops_control_and_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "LoopRun")
    client = _Client({"LoopRun": {"job_id": "job-1"}})
    context = _context(client, monkeypatch)
    for _ in range(2):
        answer = await evolution.handle_loops_run(
            context, {"loop_id": "L1"}, OPS["evolution.loops.run"]
        )
        assert answer == {"value": {"job_id": "job-1"}}
    assert client.keys == ["key-1", "key-1"]
    assert OPS["evolution.loops.run"].scopes == {"loops:control"}
    assert OPS["evolution.loops.run"].effect is Effect.WRITE
    assert OPS["evolution.loops.run"].idempotency.name == "KEY_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R022.2.5")
@pytest.mark.asyncio
async def test_pause_requires_loops_control_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "LoopPause")
    client = _Client({"LoopPause": {"state": "paused"}})
    context = _context(client, monkeypatch)
    for _ in range(2):
        answer = await evolution.handle_loops_pause(
            context, {"loop_id": "L1"}, OPS["evolution.loops.pause"]
        )
        assert answer == {"value": {"state": "paused"}}
    assert client.sent[-1] == ("LoopPause", {"loop_id": "L1"}, "tenant-content")
    assert OPS["evolution.loops.pause"].scopes == {"loops:control"}
    refusing = _context(_Client({}, refuse="NOT_FOUND"), monkeypatch)
    with pytest.raises(EngineRefusal) as unknown:
        await evolution.handle_loops_pause(
            refusing, {"loop_id": "x"}, OPS["evolution.loops.pause"]
        )
    assert unknown.value.code == "NOT_FOUND"
