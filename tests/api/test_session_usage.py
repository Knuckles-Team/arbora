"""Shared invoke forwards canonical EG session totals without recomputation."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arbora.api.invoke import OpResult, invoke
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.ops import usage
from arbora.api.registry import Registry, Surface
from tests.api._support import make_verified_caller
from tests.api.test_invoke_contract import FixtureRuntime, setup_services


def snapshot(context_id: str = "context") -> dict[str, Any]:
    return {
        "context_id": context_id,
        "counts": {"input_tokens": 19},
        "expected_runs": 3,
        "recorded_runs": 2,
        "missing_runs": 1,
        "count_coverage": {"input_tokens": 1},
        "inventory_complete": False,
    }


class SessionRuntime(FixtureRuntime):
    def __init__(self, value: dict[str, Any]):
        super().__init__()
        self.value = value
        self.reads: list[dict[str, Any]] = []
        self.principals: list[str] = []

    @asynccontextmanager
    async def as_caller(self, supplied: Any):
        self.principals.append(supplied.principal)
        yield SimpleNamespace(
            work_items=SimpleNamespace(session_usage=self.session_usage)
        )

    async def session_usage(self, **kwargs: Any) -> dict[str, Any]:
        self.reads.append(kwargs)
        return self.value

    async def dispatch(self, op: Any, params: Any, context: Any) -> Any:
        return await BoundOperationRuntime.dispatch(
            cast(BoundOperationRuntime, self), op, params, context
        )


@pytest.mark.spec("ARBORA-OPS-R024.3.1")
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
    caller = make_verified_caller("usage:read")()
    result = await invoke(
        "usage.read", {"context_id": "context"}, caller, surface, services=services
    )
    assert isinstance(result, OpResult), result
    assert result.value == {"value": {"scope": "session", **snapshot()}}
    assert runtime.reads == [{"tenant": caller.tenant, "context_id": "context"}]
    assert runtime.principals == [caller.principal]


@pytest.mark.spec("ARBORA-OPS-R024.3.1")
@pytest.mark.parametrize(
    "params",
    [
        {"context_id": "context", "tenant": "other"},
        {"context_id": "context", "principal": "other"},
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
        make_verified_caller("usage:read")(),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)
    assert runtime.reads == []


@pytest.mark.spec("ARBORA-OPS-R024.3.1")
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
        make_verified_caller("kg:read")(),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)
    assert runtime.reads == []


@pytest.mark.spec("ARBORA-OPS-R024.3.1")
@pytest.mark.parametrize(
    "change",
    [
        {"context_id": "foreign"},
        {"missing_runs": 0},
        {"count_coverage": {"input_tokens": 4}},
        {"counts": {"input_tokens": -1}},
        {"inventory_complete": "false"},
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
        make_verified_caller("usage:read")(),
        Surface.HTTP,
        services=services,
    )
    assert not isinstance(result, OpResult)
