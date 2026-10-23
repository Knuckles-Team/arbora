"""Shared invoke forwards canonical EG session totals without recomputation."""

from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arbora.api.invoke import OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime, ExecutionContext
from arbora.api.mcp.caller import caller_from_session
from arbora.api.ops import usage
from arbora.api.registry import Registry, Surface
from arbora.deployment.semantic_provisioning import CONTROL_GRAPH
from tests.a2a.test_operation_bridge import session
from tests.api.test_invoke_contract import FixtureRuntime, setup_services


def snapshot(context_id: str = "context") -> dict[str, Any]:
    return {
        "context_id": context_id,
        "counts": {"input_tokens": 19, "output_tokens": 0},
        "expected_runs": 3,
        "recorded_runs": 2,
        "missing_runs": 1,
        "count_coverage": {"input_tokens": 1, "output_tokens": 1},
        "inventory_complete": False,
    }


def verified_caller(scope: str = "usage:read") -> Any:
    bound = replace(session(frozenset({scope})), graph="caller-runtime-graph")
    return caller_from_session(bound, credential_kind="bearer")


class SessionClient:
    def __init__(self, reader: Any):
        self.work_items = SimpleNamespace(session_usage=reader)
        self.claims: ContextVar[Any] = ContextVar("session-usage-claims", default=None)
        self.authenticated = True

    @contextmanager
    def use_verified_context(self, claims: Any):
        token = self.claims.set(claims)
        try:
            yield self
        finally:
            self.claims.reset(token)


class SessionRuntime(FixtureRuntime):
    def __init__(self, value: dict[str, Any]):
        super().__init__()
        self.value = value
        self.reads: list[dict[str, Any]] = []
        self.principals: list[str] = []
        self.client = SessionClient(self.session_usage)
        self.bound_claims: list[Any] = []

    @asynccontextmanager
    async def as_caller(self, supplied: Any):
        self.principals.append(supplied.principal)
        yield self.client

    async def session_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        self.bound_claims.append(self.client.claims.get())
        return self.value

    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        return await BoundOperationRuntime.dispatch(
            cast(BoundOperationRuntime, self), op, params, context
        )


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
async def test_served_session_usage_preserves_canonical_partial_totals(
    surface: Surface,
) -> None:
    runtime = SessionRuntime(snapshot())
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    caller = verified_caller()
    result = await invoke(
        "usage.read", {"context_id": "context"}, caller, surface, services=services
    )
    assert isinstance(result, OpResult), result
    assert result.value == {"value": {"scope": "session", **snapshot()}}
    assert caller.session.graph != CONTROL_GRAPH
    assert runtime.reads == [
        {"tenant": caller.tenant, "context_id": "context", "graph": CONTROL_GRAPH}
    ]
    assert runtime.bound_claims[0]["principal"] == caller.principal
    assert runtime.bound_claims[0]["tenant"] == caller.tenant
    assert runtime.client.claims.get() is None
    assert runtime.principals == [caller.principal]


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize(
    "params",
    [
        {"context_id": "context", "tenant": "other"},
        {"context_id": "context", "principal": "other"},
        {"context_id": "context", "graph": "other"},
    ],
)
async def test_session_usage_cannot_supply_authority(params: dict[str, Any]) -> None:
    runtime = SessionRuntime(snapshot())
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read",
        params,
        verified_caller(),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)
    assert runtime.reads == []


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
async def test_session_usage_missing_scope_has_zero_reads() -> None:
    runtime = SessionRuntime(snapshot())
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read",
        {"context_id": "context"},
        verified_caller("kg:read"),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)
    assert runtime.reads == []


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize(
    "change",
    [
        {"context_id": "foreign"},
        {"missing_runs": 0},
        {"count_coverage": {"input_tokens": 4}},
        {"counts": {"input_tokens": -1}},
        {"inventory_complete": "false"},
        {"counts": {"requests": True}, "count_coverage": {"requests": 1}},
        {"counts": {"requests": 1.5}, "count_coverage": {"requests": 1}},
        {"counts": {"requests": "1"}, "count_coverage": {"requests": 1}},
        {
            "counts": {"requests": 9_007_199_254_740_992},
            "count_coverage": {"requests": 2},
        },
        {"counts": {"requests": 0}, "count_coverage": {"requests": 0}},
        {"counts": {}, "count_coverage": {}},
        {"count_coverage": {"input_tokens": 1}},
        {"counts": {"cost": 12}, "count_coverage": {"cost": 2}},
        {"expected_runs": True},
        {"expected_runs": 10_001, "missing_runs": 9_999},
    ],
)
async def test_session_usage_refuses_foreign_or_malformed_results(
    change: dict[str, Any],
) -> None:
    runtime = SessionRuntime({**snapshot(), **change})
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read",
        {"context_id": "context"},
        verified_caller(),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
@pytest.mark.parametrize(
    "counts,recorded,coverage",
    [
        ({}, 0, {}),
        ({"requests": 0}, 1, {"requests": 1}),
        ({"total_tokens": 9_007_199_254_740_991}, 1, {"total_tokens": 1}),
    ],
)
async def test_missing_zero_and_safe_precision_are_distinct(
    surface: Surface, counts: dict[str, int], recorded: int, coverage: dict[str, int]
) -> None:
    value = {
        "context_id": "context",
        "counts": counts,
        "expected_runs": 1,
        "recorded_runs": recorded,
        "missing_runs": 1 - recorded,
        "count_coverage": coverage,
        "inventory_complete": True,
    }
    runtime = SessionRuntime(value)
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read",
        {"context_id": "context"},
        verified_caller(),
        surface,
        services=services,
    )
    assert isinstance(result, OpResult), result
    assert result.value == {"value": {"scope": "session", **value}}


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize("surface", [Surface.HTTP, Surface.MCP])
async def test_native_engine_denial_remains_typed_and_private(surface: Surface) -> None:
    from epistemic_graph import EngineResponseError

    class DeniedRuntime(SessionRuntime):
        async def session_usage(self, **kwargs: Any) -> dict[str, Any]:
            raise EngineResponseError("ACCESS_DENIED", "private tenant mismatch")

    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=DeniedRuntime(snapshot()),
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read",
        {"context_id": "context"},
        verified_caller(),
        surface,
        services=services,
    )
    assert not isinstance(result, OpResult)
    assert result.code == "ACCESS_DENIED"
    assert result.source == "engine"
    assert "private" not in str(result)


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
async def test_missing_native_reader_refuses_instead_of_tenant_fallback() -> None:
    from arbora.api.invoke.pipeline import OperationRefused

    caller = verified_caller()
    context = ExecutionContext(caller, SimpleNamespace(), caller.principal, False, {})
    with pytest.raises(OperationRefused) as refused:
        await usage.handle_usage_read(
            context, {"context_id": "context"}, usage.operations()[0]
        )
    assert refused.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize("change", [{"service_identity": True}, {"owner": "foreign"}])
async def test_session_reader_refuses_service_or_changed_owner(change):
    from arbora.api.invoke.pipeline import OperationRefused

    caller = verified_caller()
    runtime = SessionRuntime(snapshot())
    context = ExecutionContext(caller, runtime.client, caller.principal, False, {})
    with pytest.raises(OperationRefused):
        await usage.handle_usage_read(
            replace(context, **change), {"context_id": "context"}, usage.operations()[0]
        )
    assert runtime.reads == []


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
async def test_explicit_anonymous_reader_keeps_transport_unverified(monkeypatch):
    from arbora.security import anonymous

    monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "none")
    caller = caller_from_session(anonymous.session(), credential_kind="none")
    runtime = SessionRuntime(snapshot())
    runtime.client.authenticated = False
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read", {"context_id": "context"}, caller, Surface.HTTP, services=services
    )
    assert isinstance(result, OpResult), result
    assert runtime.reads == [
        {"tenant": "local", "context_id": "context", "graph": CONTROL_GRAPH}
    ]
    assert runtime.bound_claims == [None]


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize(
    "failure",
    [
        ImportError("generated session reader is not published"),
        AttributeError("generated session reader is not published"),
    ],
)
async def test_unpublished_generated_reader_remains_unavailable(failure):
    class UnpublishedRuntime(SessionRuntime):
        async def session_usage(self, **kwargs: Any) -> dict[str, Any]:
            raise failure

    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=UnpublishedRuntime(snapshot()),
        external_effect_operations=frozenset(),
    )
    result = await invoke(
        "usage.read",
        {"context_id": "context"},
        verified_caller(),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)
    assert result.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R024.3.1.1")
@pytest.mark.parametrize("change", ["revocation", "graph", "principal"])
async def test_authority_change_during_native_read_never_releases_snapshot(change):
    import asyncio

    entered, release = asyncio.Event(), asyncio.Event()

    class DelayedRuntime(SessionRuntime):
        async def session_usage(self, **kwargs: Any) -> dict[str, Any]:
            entered.set()
            await release.wait()
            return await super().session_usage(**kwargs)

    runtime = DelayedRuntime(snapshot())
    caller = verified_caller()
    services, *_ = setup_services(
        registry=Registry(usage.operations()),
        runtime=runtime,
        external_effect_operations=frozenset(),
    )
    pending = asyncio.create_task(
        invoke(
            "usage.read",
            {"context_id": "context"},
            caller,
            Surface.HTTP,
            services=services,
        )
    )
    await entered.wait()
    if change == "revocation":
        caller.session.actor.credential_lease.revoke("fixture revocation")
    elif change == "graph":
        object.__setattr__(caller.session, "graph", "changed-graph")
    else:
        object.__setattr__(caller.session.actor, "actor_id", "changed-principal")
    release.set()
    result = await pending
    assert not isinstance(result, OpResult)
    assert result.code == "UNAUTHENTICATED"
    assert runtime.reads == [
        {"tenant": "tenant:one", "context_id": "context", "graph": CONTROL_GRAPH}
    ]
    assert runtime.client.claims.get() is None
