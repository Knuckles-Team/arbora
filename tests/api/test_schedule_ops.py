"""The schedule operations forward to the engine job scheduler (ARBORA-OPS-R022.2.6 to R022.2.11)."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import medulla.generated as generated
import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import _engine, schedules
from arbora.api.registry import Effect, Registry, Surface

OPS = {op.id: op for op in schedules.operations()}


class _Client:
    """A structural double for a medulla client that serves the job scheduler."""

    authenticated = True

    def __init__(self, answer: Any = None, refuse: str | None = None) -> None:
        self.sent: list[tuple[str, Any, str]] = []
        self.claims: list[dict[str, Any]] = []
        self._answer = {"name": "nightly"} if answer is None else answer
        self._refuse = refuse

    def use_verified_context(self, claims: Any) -> Any:
        self.claims.append(dict(claims))
        return contextlib.nullcontext()

    async def _send(
        self, method: str, params: Any, graph: str, **_keywords: Any
    ) -> Any:
        self.sent.append((method, params, graph))
        if self._refuse is not None:
            from medulla import EngineResponseError

            raise EngineResponseError(self._refuse, "refused")
        return self._answer


def _context(client: _Client, monkeypatch: pytest.MonkeyPatch) -> Any:
    async def graph(_context: Any) -> str:
        return "tenant-content"

    monkeypatch.setattr(_engine, "caller_dispatch_graph", graph)
    monkeypatch.setattr(schedules, "caller_dispatch_graph", graph)
    caller = SimpleNamespace(
        tenant="tenant-a", principal="alice", engine_claims={"principal": "alice"}
    )
    return SimpleNamespace(
        caller=caller, client=client, idempotency_key="key-1", services={}
    )


def _serve(monkeypatch: pytest.MonkeyPatch, *extra: str) -> None:
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"AnalyticsJob", *extra},
    )


def test_operations_declare_scopes_effects_and_keys() -> None:
    Registry(tuple(OPS.values()))
    for op_id in (
        "schedules.create",
        "schedules.update",
        "schedules.pause",
        "schedules.resume",
    ):
        assert OPS[op_id].scopes == {"loops:control"}
        assert OPS[op_id].effect is Effect.WRITE
        assert OPS[op_id].idempotency.name == "KEY_REQUIRED"
    assert OPS["schedules.runs"].scopes == {"loops:read"}
    assert OPS["schedules.runs"].effect is Effect.READ
    assert OPS["schedules.delete"].effect is Effect.ADMIN
    assert Surface.CONSOLE in OPS["schedules.delete"].surfaces


@pytest.mark.spec("ARBORA-OPS-R022.2.6")
@pytest.mark.asyncio
async def test_create_requires_loops_control_key_and_forwards_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    client = _Client()
    context = _context(client, monkeypatch)
    params = schedules.ScheduleWriteParams(
        name="nightly", task_kind="agent_run", spec={"cron": "0 3 * * *"}, max_tokens=10
    ).model_dump()
    answer = await schedules.handle_schedule(context, params, OPS["schedules.create"])
    assert answer == {"value": {"name": "nightly"}}
    method, sent, graph = client.sent[0]
    assert (method, graph) == ("AnalyticsJob", "tenant-content")
    body = sent["op"]["ScheduleCreate"]
    assert body["graph"] == "tenant-content" and body["max_tokens"] == 10
    assert "period_s" not in body
    assert client.claims == [{"principal": "alice"}]
    # The registry declares the scope and the key; the pipeline enforces both.
    assert OPS["schedules.create"].scopes == {"loops:control"}
    assert OPS["schedules.create"].idempotency.name == "KEY_REQUIRED"


@pytest.mark.spec("ARBORA-OPS-R022.2.6")
@pytest.mark.asyncio
async def test_absent_engine_method_fails_closed_with_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        generated, "METHOD_IDS", frozenset(generated.METHOD_IDS) - {"AnalyticsJob"}
    )
    context = _context(_Client(), monkeypatch)
    with pytest.raises(OperationRefused) as refused:
        await schedules.handle_schedule(context, {"name": "n"}, OPS["schedules.pause"])
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R022.2.7")
@pytest.mark.asyncio
async def test_delete_requires_step_up_and_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    assert OPS["schedules.delete"].effect is Effect.ADMIN
    assert OPS["schedules.delete"].principals.name == "HUMAN_UNDELEGATED"
    assert Surface.CONSOLE in OPS["schedules.delete"].surfaces
    client = _Client(refuse="SCHEDULE_NOT_FOUND")
    context = _context(client, monkeypatch)
    with pytest.raises(EngineRefusal) as refused:
        await schedules.handle_schedule(
            context, {"name": "foreign"}, OPS["schedules.delete"]
        )
    assert refused.value.code == "SCHEDULE_NOT_FOUND"
    assert client.sent[0][1] == {
        "op": {"ScheduleDelete": {"name": "foreign", "graph": "tenant-content"}}
    }


@pytest.mark.spec("ARBORA-OPS-R022.2.8")
@pytest.mark.asyncio
async def test_pause_and_resume_are_scoped_and_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    client = _Client()
    context = _context(client, monkeypatch)
    for op_id, engine_op in (
        ("schedules.pause", "SchedulePause"),
        ("schedules.resume", "ScheduleResume"),
    ):
        assert OPS[op_id].scopes == {"loops:control"}
        assert OPS[op_id].idempotency.name == "KEY_REQUIRED"
        for _ in range(2):
            await schedules.handle_schedule(context, {"name": "n"}, OPS[op_id])
        assert client.sent[-1][1] == {
            "op": {engine_op: {"name": "n", "graph": "tenant-content"}}
        }


@pytest.mark.spec("ARBORA-OPS-R022.2.9")
@pytest.mark.asyncio
async def test_runs_returns_engine_tick_history_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    ticks = {"ticks": [{"outcome": "skipped", "reason": "group_active"}]}
    monkeypatch.setattr(generated.models, "JobOpScheduleTicks", object, raising=False)
    context = _context(_Client(answer=ticks), monkeypatch)
    answer = await schedules.handle_schedule(
        context, {"name": "n", "limit": 5}, OPS["schedules.runs"]
    )
    assert answer == {"value": ticks}
    assert OPS["schedules.runs"].scopes == {"loops:read"}


@pytest.mark.spec("ARBORA-OPS-R022.2.9")
@pytest.mark.asyncio
async def test_pinned_engine_refuses_runs_naming_the_missing_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    monkeypatch.delattr(generated.models, "JobOpScheduleTicks", raising=False)
    client = _Client()
    with pytest.raises(OperationRefused) as refused:
        await schedules.handle_schedule(
            _context(client, monkeypatch),
            {"name": "n", "limit": 5},
            OPS["schedules.runs"],
        )
    assert refused.value.code == "UNAVAILABLE"
    assert refused.value.details["missing"] == "JobOpScheduleTicks"
    assert client.sent == []


@pytest.mark.spec("ARBORA-OPS-R022.2.11")
@pytest.mark.asyncio
async def test_exclusion_group_and_spacing_are_forwarded_and_engine_refusal_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    for bad in ({"exclusion_group": ""}, {"min_spacing_s": 604_801}):
        with pytest.raises(ValueError):
            schedules.ScheduleWriteParams(
                name="n", task_kind="k", spec="0 * * * *", **bad
            )
    params = schedules.ScheduleWriteParams(
        name="n",
        task_kind="k",
        spec="0 * * * *",
        exclusion_group="heavy",
        min_spacing_s=60,
    ).model_dump()
    client = _Client()
    await schedules.handle_schedule(
        _context(client, monkeypatch), params, OPS["schedules.update"]
    )
    body = client.sent[0][1]["op"]["ScheduleUpdate"]
    assert (body["exclusion_group"], body["min_spacing_s"]) == ("heavy", 60)
    refusing = _Client(refuse="SCHEDULE_INVALID")
    with pytest.raises(EngineRefusal) as refused:
        await schedules.handle_schedule(
            _context(refusing, monkeypatch), params, OPS["schedules.create"]
        )
    assert refused.value.code == "SCHEDULE_INVALID"


def _library_answer(agent_id: str = "inst-1") -> dict[str, Any]:
    return {
        "entry": {
            "agent_id": agent_id,
            "runtime": {
                "usage_limits": {"input_tokens_limit": 100, "output_tokens_limit": 50}
            },
        }
    }


class _LibraryClient(_Client):
    def __init__(self, library: Any) -> None:
        super().__init__()
        self._library = library

    async def _send(self, method: str, params: Any, graph: str, **keywords: Any) -> Any:
        if method == "AgentLibrary":
            self.sent.append((method, params, graph))
            return self._library
        return await super()._send(method, params, graph, **keywords)


@pytest.mark.spec("ARBORA-OPS-R022.2.12")
@pytest.mark.asyncio
async def test_role_target_inherits_binding_budget_and_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "AgentLibrary")
    spec = {"role_instance_id": "inst-1"}
    client = _LibraryClient(_library_answer())
    inherited = schedules.ScheduleWriteParams(
        name="n", task_kind="roles.run", spec=spec
    ).model_dump()
    await schedules.handle_schedule(
        _context(client, monkeypatch), inherited, OPS["schedules.create"]
    )
    read = client.sent[0]
    assert read[0] == "AgentLibrary"
    assert read[1]["op"] == {
        "operation": "current",
        "agent_id": "inst-1",
        "tenant_id": "tenant-a",
    }
    body = client.sent[1][1]["op"]["ScheduleCreate"]
    assert (body["max_tokens"], body["exclusion_group"]) == (150, "inst-1")
    # The caller's own values win and a non-role target never reads the library.
    own = schedules.ScheduleWriteParams(
        name="n",
        task_kind="roles.run",
        spec=spec,
        max_tokens=7,
        exclusion_group="mine",
        min_spacing_s=30,
    ).model_dump()
    client = _LibraryClient(_library_answer())
    await schedules.handle_schedule(
        _context(client, monkeypatch), own, OPS["schedules.update"]
    )
    body = client.sent[1][1]["op"]["ScheduleUpdate"]
    assert (body["max_tokens"], body["exclusion_group"], body["min_spacing_s"]) == (
        7,
        "mine",
        30,
    )
    plain = schedules.ScheduleWriteParams(
        name="n", task_kind="agent_run", spec="0 * * * *"
    ).model_dump()
    client = _LibraryClient(_library_answer())
    await schedules.handle_schedule(
        _context(client, monkeypatch), plain, OPS["schedules.create"]
    )
    assert [sent[0] for sent in client.sent] == ["AnalyticsJob"]


@pytest.mark.spec("ARBORA-OPS-R022.2.12")
@pytest.mark.asyncio
async def test_role_target_refuses_absent_or_foreign_instance_with_no_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "AgentLibrary")
    for library in ({}, _library_answer("someone-else")):
        client = _LibraryClient(library)
        params = schedules.ScheduleWriteParams(
            name="n", task_kind="roles.run", spec={"role_instance_id": "inst-1"}
        ).model_dump()
        with pytest.raises(OperationRefused) as refused:
            await schedules.handle_schedule(
                _context(client, monkeypatch), params, OPS["schedules.create"]
            )
        assert refused.value.code == "NOT_FOUND"
        assert [sent[0] for sent in client.sent] == ["AgentLibrary"]
    missing = schedules.ScheduleWriteParams(
        name="n", task_kind="roles.run", spec={"x": 1}
    ).model_dump()
    with pytest.raises(OperationRefused) as refused:
        await schedules.handle_schedule(
            _context(_LibraryClient({}), monkeypatch), missing, OPS["schedules.create"]
        )
    assert refused.value.code == "INVALID_ARGUMENT"
