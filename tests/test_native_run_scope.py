"""Native A2A lease authority stays inside the host during AU execution."""

from contextvars import copy_context
from types import SimpleNamespace
from typing import Any, cast

import pytest
from agent_utilities import host_ports

from arbora import agent_runs, work_items
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.core.settings import runtime_config

pytestmark = pytest.mark.spec("ARBORA-OPS-R001")
SESSION = SimpleNamespace(
    tenant="tenant",
    graph="graph",
    engine_verified_context=lambda: {
        "principal": "test-principal",
        "tenant": "tenant",
        "audience": "test-audience",
        "agent_id": "test-principal",
        "roles": [],
        "scopes": ["kg:read", "kg:write"],
        "policy_version": "test-policy",
        "delegation": [],
    },
)
CLAIM = {
    "claimed": True,
    "tenant": "tenant",
    "work_item_id": "work",
    "lease_holder_ref": "private-native-worker",
    "lease_epoch": 4,
    "fencing_token": 938491,
    "lease_expires_at_ms": 2**63 - 1,
}


def scope(**changes: Any) -> Any:
    return agent_runs.native_run_scope(
        **{
            "run_id": "run",
            "task_id": "task",
            "context_id": "context",
            "work_item_id": "work",
            "session": SESSION,
            "claim": CLAIM,
            **changes,
        }
    )


@pytest.mark.asyncio
async def test_real_a2a_run_exposes_claim_only_to_host_planning(
    monkeypatch: Any,
) -> None:
    seen: list[Any] = []
    events: list[Any] = []
    commits: list[Any] = []
    monkeypatch.setattr(runtime_config, "model_registry_path", None)
    monkeypatch.setattr(runtime_config, "chat_models", [])
    monkeypatch.setattr(agent_runs, "run_config", lambda: {"agent_model": "test"})
    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: dict(CLAIM))

    def commit(*args: Any, **kwargs: Any) -> str:
        commits.append((args, kwargs))
        return "committed"

    monkeypatch.setattr(work_items, "commit_result", commit)

    def decide(request: Any) -> Any:
        owner = agent_runs.current_native_run(request)
        assert owner is not None
        assert owner.claim == CLAIM
        assert (owner.run_id, owner.task_id, owner.context_id, owner.work_item_id) == (
            "run",
            "task",
            "context",
            "work",
        )
        assert request.metadata == {"session": SESSION}
        assert request.task == "hello"
        seen.append(owner)
        return agent_runs.decide_run(request)

    host_ports.register_run_decision_provider(decide)
    host_ports.register_run_event_sink(events.append)
    try:
        result = await WorkItemA2AAuthority(lambda: object())._run(
            object(),
            item_id="work",
            session=SESSION,
            agent_name="helper",
            task="hello",
            task_id="task",
            context_id="context",
            run_id="run",
        )
    finally:
        host_ports.register_run_decision_provider(None)
        host_ports.register_run_event_sink(None)
    assert seen and result and commits[0][1]["outcome"] == "succeeded"
    assert commits[0][0][2] == CLAIM
    assert agent_runs.current_native_run(SimpleNamespace(run_id="run")) is None
    serialized = repr(events)
    for private in ("private-native-worker", "938491", "fencing_token", "lease_epoch"):
        assert private not in serialized
        assert private not in repr(seen[0])


def test_scope_resets_revokes_copies_and_keeps_fence_immutable() -> None:
    request = SimpleNamespace(run_id="run")
    with scope():
        copied = copy_context()
        owner = agent_runs.current_native_run(request)
        assert owner is not None
        with pytest.raises(TypeError):
            cast(Any, owner.claim)["fencing_token"] = 0
    assert agent_runs.current_native_run(request) is None
    with pytest.raises(RuntimeError, match="scope is closed"):
        copied.run(agent_runs.current_native_run, request)


@pytest.mark.parametrize(
    "run_request",
    [
        SimpleNamespace(run_id="foreign"),
        SimpleNamespace(run_id="run", metadata={"session": object()}),
    ],
)
def test_scope_refuses_foreign_run_or_session(run_request: Any) -> None:
    with scope(), pytest.raises(RuntimeError, match="does not match"):
        agent_runs.current_native_run(run_request)


def test_model_metadata_never_supplies_authority() -> None:
    request = SimpleNamespace(run_id="run", metadata={"native_claim": CLAIM})
    assert agent_runs.current_native_run(request) is None


@pytest.mark.parametrize(
    "change",
    [
        {"claimed": False},
        {"tenant": "foreign"},
        {"lease_holder_ref": ""},
        {"lease_holder_ref": "   "},
        {"lease_holder_ref": None},
        {"lease_epoch": True},
        {"lease_epoch": 0},
        {"lease_epoch": "1"},
        {"fencing_token": 0},
        {"fencing_token": True},
        {"fencing_token": -1},
    ],
)
def test_invalid_claim_never_enters_scope(change: Any) -> None:
    with (
        pytest.raises(RuntimeError, match="claim is invalid"),
        scope(claim={**CLAIM, **change}),
    ):
        pytest.fail("invalid scope entered")


@pytest.mark.asyncio
async def test_cancellation_revokes_claim_scope() -> None:
    import asyncio

    copied = None
    with pytest.raises(asyncio.CancelledError):
        with scope():
            copied = copy_context()
            raise asyncio.CancelledError
    assert copied is not None
    with pytest.raises(RuntimeError, match="scope is closed"):
        copied.run(agent_runs.current_native_run, SimpleNamespace(run_id="run"))


@pytest.mark.asyncio
async def test_concurrent_runs_do_not_share_claims() -> None:
    import asyncio

    async def read(run_id: str, fence: int) -> tuple[str, int]:
        with scope(run_id=run_id, claim={**CLAIM, "fencing_token": fence}):
            await asyncio.sleep(0)
            owner = agent_runs.current_native_run(SimpleNamespace(run_id=run_id))
            assert owner is not None
            return owner.run_id, owner.claim["fencing_token"]

    assert await asyncio.gather(read("one", 1), read("two", 2)) == [
        ("one", 1),
        ("two", 2),
    ]


@pytest.mark.asyncio
async def test_a2a_binds_actual_engine_graph_not_caller_default(
    monkeypatch: Any,
) -> None:
    from arbora.mcp_server.task_plan import TaskPlanConfig

    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: dict(CLAIM))
    monkeypatch.setattr(work_items, "commit_result", lambda *a, **k: "committed")
    seen = []

    async def execute(request: Any, **kwargs: Any) -> Any:
        owner = agent_runs.current_native_run(request)
        assert owner is not None
        seen.append(owner.owner_graph)
        assert owner.session.graph == "graph"
        return SimpleNamespace(run_id="run", output="done", usage={})

    authority = WorkItemA2AAuthority(
        lambda: None,
        task_plan_config=TaskPlanConfig({}, {}, 1, 2),
        hosted_invocation=object(),
    )
    monkeypatch.setattr(
        authority, "_agent_executor", lambda: SimpleNamespace(execute_agent=execute)
    )
    await authority._run(
        SimpleNamespace(work_item_graph="owner-graph"),
        item_id="work",
        session=SESSION,
        agent_name="helper",
        task="hello",
        task_id="task",
        run_id="run",
    )
    assert seen == ["owner-graph"]


@pytest.mark.parametrize("owner_graph", ["", " graph", "graph ", 1, False])
def test_native_scope_refuses_inexact_owner_graph(owner_graph: Any) -> None:
    with (
        pytest.raises(RuntimeError, match="claim is invalid"),
        scope(owner_graph=owner_graph),
    ):
        pytest.fail("invalid graph entered scope")


@pytest.mark.parametrize("owner_graph", [None, "private-owner-graph"])
def test_native_scope_retains_exact_owner_graph(owner_graph: str | None) -> None:
    with scope(owner_graph=owner_graph):
        owner = agent_runs.current_native_run(SimpleNamespace(run_id="run"))
        assert owner is not None
        assert owner.owner_graph == owner_graph
