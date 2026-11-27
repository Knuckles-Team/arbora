"""Provisioning ports that act through the fleet (ARBORA-FLEET-R024.12, R024.13).

The engine makes no connector call, so a scale or a rollback is one governed
``fleet.call`` to the container-manager connector. The tools are the connector's
served ones: ``cm_multi_context`` (action ``scale_deployment``) and ``cm_k8s_workloads``
(actions ``scale_statefulset`` and ``rollout_undo``). A target names its workload as
``<namespace>/<name>``; the runtime id selects the workload kind. Replica counts are
absolute, so a replay of one execution key converges on the same state.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from collections.abc import Callable, Iterator, Mapping
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.core.environment import read_env
from arbora.fleet.fleet_tool_call import call_fleet_tool, refused_result
from arbora.fleet.scaling.scaling_intent_reconciler import (
    ScaleActuationRequest,
    ScaleActuationResult,
    ScaleIntentState,
)

SERVER_SETTING = "ARBORA_CONTAINER_MANAGER_FLEET_SERVER"
DEFAULT_SERVER = "container-manager-mcp"
WORKLOADS_TOOL = "cm_k8s_workloads"
MULTI_CONTEXT_TOOL = "cm_multi_context"

#: The runtime ids an actuation target may carry, and the workload kind each scales.
RUNTIMES = {"deployment": "scale_deployment", "statefulset": "scale_statefulset"}

#: The one request-bound dispatcher the sync actuator reads: ``(context, loop)``.
_REQUEST: contextvars.ContextVar[tuple[Any, asyncio.AbstractEventLoop] | None] = (
    contextvars.ContextVar("fleet_actuation_request", default=None)
)


@contextlib.contextmanager
def bind_request(context: Any) -> Iterator[None]:
    """Bind the running request to the actuator for the span of one reconcile."""
    token = _REQUEST.set((context, asyncio.get_running_loop()))
    try:
        yield
    finally:
        _REQUEST.reset(token)


def request_context() -> Any:
    """The verified request the running reconcile serves; refuses when none is bound."""
    bound = _REQUEST.get()
    if bound is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "no request is bound"})
    return bound[0]


def _server() -> str:
    return str(read_env(SERVER_SETTING, DEFAULT_SERVER))


def split_workload(workload_id: str) -> tuple[str, str]:
    """``<namespace>/<name>``; a bare name is in the ``default`` namespace."""
    namespace, _, name = workload_id.rpartition("/")
    return namespace or "default", name


def scale_call(request: ScaleActuationRequest) -> tuple[str, dict[str, Any]]:
    """The connector tool and arguments that set one target's replica count."""
    namespace, name = split_workload(request.target.workload_id)
    action = RUNTIMES.get(request.target.runtime_id)
    if action is None:
        raise ValueError(f"no scale tool for runtime {request.target.runtime_id}")
    arguments: dict[str, Any] = {
        "action": action,
        "name": name,
        "namespace": namespace,
        "replicas": request.desired_replicas,
    }
    if action == "scale_deployment":
        arguments["context"] = request.target.cluster_id
        return MULTI_CONTEXT_TOOL, arguments
    return WORKLOADS_TOOL, arguments


def rollback_call(service: str) -> tuple[str, dict[str, Any]]:
    """The connector tool and arguments that undo the last rollout of one service."""
    namespace, name = split_workload(service)
    return WORKLOADS_TOOL, {
        "action": "rollout_undo",
        "resource_type": "deployment",
        "resource_name": name,
        "namespace": namespace,
    }


async def deploy_rollback(context: Any, service: str, detail: str) -> None:
    """Roll one service back through ``fleet.call``; a refusing connector refuses."""
    del detail
    tool, arguments = rollback_call(service)
    result = await call_fleet_tool(context, _server(), tool, arguments)
    if refused_result(result):
        raise OperationRefused(
            "UPSTREAM_FAILED", {"reason": "the rollback was refused"}
        )


def _failed(
    request: ScaleActuationRequest, code: str, detail: str, *, retryable: bool
) -> ScaleActuationResult:
    return ScaleActuationResult(
        execution_key=request.execution_key,
        target=request.target,
        state=ScaleIntentState.FAILED,
        scaled=False,
        retryable=retryable,
        error_code=code,
        detail=detail,
    )


def _observed_replicas(result: Any, desired: int) -> int | None:
    """The replica count the connector reports, or ``None`` when it reports none."""
    if isinstance(result, Mapping):
        for key in ("replicas", "spec_replicas", "observed_replicas"):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
    return None


class FleetScaleActuator:
    """Apply one typed scale request through the fleet; replays converge on one count."""

    name = "fleet-container-manager"

    def __init__(
        self,
        dispatch: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self._dispatch = dispatch or _dispatch_on_request_loop

    def apply(self, request: ScaleActuationRequest) -> ScaleActuationResult:
        if request.dry_run:
            return ScaleActuationResult(
                execution_key=request.execution_key,
                target=request.target,
                state=ScaleIntentState.SIMULATED,
                scaled=False,
                detail="dry run: no connector call was made",
            )
        try:
            tool, arguments = scale_call(request)
        except ValueError as exc:
            return _failed(request, "unsupported_runtime", str(exc), retryable=False)
        try:
            result = self._dispatch(_server(), tool, arguments)
        except OperationRefused as exc:
            return _failed(
                request, exc.code.lower(), "the fleet refused", retryable=False
            )
        except Exception as exc:  # noqa: BLE001 - an unreachable connector is retryable
            return _failed(
                request, "fleet_call_failed", type(exc).__name__, retryable=True
            )
        if refused_result(result):
            return _failed(request, "connector_refused", "refused", retryable=False)
        observed = _observed_replicas(result, request.desired_replicas)
        if observed is None:
            return _failed(
                request, "replicas_not_reported", "no replica count", retryable=True
            )
        return ScaleActuationResult(
            execution_key=request.execution_key,
            target=request.target,
            state=ScaleIntentState.SUCCEEDED,
            scaled=True,
            observed_replicas=observed,
            detail="scaled through the fleet",
        )


def _dispatch_on_request_loop(
    server: str, tool: str, arguments: Mapping[str, Any]
) -> Any:
    """Run one fleet call on the loop of the request that started this reconcile."""
    bound = _REQUEST.get()
    if bound is None:
        raise OperationRefused("UNAVAILABLE", {"reason": "no request is bound"})
    context, loop = bound
    future = asyncio.run_coroutine_threadsafe(
        call_fleet_tool(context, server, tool, arguments), loop
    )
    return future.result()
