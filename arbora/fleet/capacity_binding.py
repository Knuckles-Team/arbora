"""Bind the capacity controller to the served composition (ARBORA-CAPACITY-R001.4).

One registry, one outcome recorder and one limiter are created at start-up and
bound as the services the ``fleet.call`` handler and the capacity operations
read. The controller loop runs as one background task. The engine port speaks
the capacity methods through the same verified client as the other background
workers. A raise waits for an engine verdict, so with no ``capacity_ask`` bound
the loop can only narrow.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from arbora.fleet.capacity_proposals import CapacityCellView, Judge
from arbora.fleet.error_budget import Partition
from arbora.fleet.throttle_service import (
    ControllerConfig,
    ModelCapacityGate,
    OutcomeRecorder,
    PartitionLimiter,
    ThrottleController,
    ThrottleRegistry,
)

#: ``(method, params, tenant) -> engine result``.
Send = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def cell_id_of(partition: Partition) -> str:
    """The engine cell that bounds one child's calls."""
    return f"fleet:{partition.child}"


class EngineCapacityPort:
    """``CapacityPort`` over the engine's capacity methods."""

    def __init__(self, send: Send, *, clock_ms: Callable[[], int]) -> None:
        self._send, self._clock_ms = send, clock_ms

    async def cell(self, partition: Partition) -> CapacityCellView:
        status = await self._send(
            "CapacityStatus",
            {
                "request": {
                    "cell_id": cell_id_of(partition),
                    "max_count": 64,
                    "schema_version": "1",
                    "tenant_ref": partition.tenant,
                }
            },
            partition.tenant,
        )
        cells = status.get("cells") if isinstance(status, Mapping) else None
        if not cells:
            raise LookupError("the engine reports no capacity cell")
        raw = dict(cells[0])
        held = sum(
            int(lease["amount"])
            for lease in status.get("leases") or ()
            if str(lease.get("state")) not in {"released", "expired", "reclaimed"}
        )
        capacity = int(raw["capacity"])
        return CapacityCellView(
            cell_id=str(raw["cell_id"]),
            resource_class_iri=str(raw.get("resource_class")),
            unit="slots",
            capacity=capacity,
            min_capacity=int(raw.get("reserved_floor", 0)),
            max_capacity=capacity,
            measured_load=held,
            available_headroom=max(capacity - held, 0),
            premise_revision=int(raw["epoch"]),
            raw=raw,
        )

    async def update_cell(self, cell: CapacityCellView, capacity: int) -> str:
        answer = await self._send(
            "UpdateCapacityCell",
            {
                "request": {
                    "cell": {**cell.raw, "capacity": capacity},
                    "expected_epoch": cell.premise_revision,
                    "now_ms": self._clock_ms(),
                    "schema_version": "1",
                }
            },
            str(cell.raw.get("tenant_ref", "")),
        )
        decision = answer.get("decision") if isinstance(answer, Mapping) else None
        return "stale_epoch" if decision == "stale_epoch" else "accepted"

    async def throttle_cell(
        self, cell: CapacityCellView, errors: int, requests: int, window_end_ms: int
    ) -> None:
        await self._send(
            "ThrottleCapacityCell",
            {
                "request": {
                    "cell_id": cell.cell_id,
                    "now_ms": self._clock_ms(),
                    "sample": {
                        "errors": errors,
                        "requests": requests,
                        "window_end_ms": window_end_ms,
                    },
                    "schema_version": "1",
                }
            },
            str(cell.raw.get("tenant_ref", "")),
        )


ModelDecide = Callable[[dict[str, Any]], Awaitable[Any]]
Observations = Callable[[str], Awaitable[tuple[list[dict[str, Any]], dict[str, Any]]]]


def model_capacity_ask(
    decide: ModelDecide,
    observations: Observations,
    *,
    tenant: str,
    feature_schema: Mapping[str, Any],
) -> Callable[[str], Awaitable[Mapping[str, Any] | None]]:
    """The Medulla client adapter call for ``model_capacity`` (R752).

    ``observations(endpoint)`` returns the recorded call observations and the
    declared bounds the question carries. The answer is the engine's
    ``capacity:<json>`` option; nothing is computed here.
    """

    async def ask(endpoint: str) -> Mapping[str, Any] | None:
        from medulla.decision_client import (
            DeclaredOption,
            decide_request,
            declared,
            param,
        )

        recorded, bounds = await observations(endpoint)
        request = decide_request(
            tenant,
            (f"capacity.model.{endpoint}", "model_capacity", "ordinary"),
            declared([DeclaredOption("capacity")]),
            feature_schema,
            params=[
                param("capacity_observations", "text", json.dumps(recorded)),
                param("capacity_bounds", "text", json.dumps(bounds)),
            ],
        )
        answer = await decide(request)
        record = (answer.get("records") or [None])[0]
        scores = ((record or {}).get("outcome") or {}).get("scores") or []
        option = (scores[0] or {}).get("option_id") if scores else None
        if not isinstance(option, str) or not option.startswith("capacity:"):
            return None
        parsed = json.loads(option.removeprefix("capacity:"))
        return parsed if isinstance(parsed, Mapping) else None

    return ask


class CapacityRunner:
    """Runs the controller loop as one background task."""

    def __init__(
        self,
        controller: ThrottleController,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._controller, self._sleep = controller, sleep
        self._task: asyncio.Future[None] | None = None
        self._stopped = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.ensure_future(
                self._controller.run(self._sleep, lambda: not self._stopped)
            )

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


async def _no_model_decision(endpoint: str) -> Mapping[str, Any] | None:
    """No engine ask is bound: the model gate answers its fallback limit."""
    return None


def _section(raw: object) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def with_capacity(
    bindings: Mapping[str, Any],
    send: Send,
    *,
    aimd_section: object = None,
    clock_ms: Callable[[], int] | None = None,
) -> tuple[Mapping[str, Any], Callable[[], Awaitable[None]]]:
    """Bind the registry, recorder and limiter; return them and the start-up step."""
    clock = clock_ms or (lambda: int(time.time() * 1000))
    registry = ThrottleRegistry()
    recorder = OutcomeRecorder()
    ask: Judge | None = bindings.get("capacity_ask")
    model_ask = bindings.get("model_capacity_ask")
    controller = ThrottleController(
        registry=registry,
        recorder=recorder,
        port=EngineCapacityPort(send, clock_ms=clock),
        config=ControllerConfig.from_section(_section(aimd_section)),
        clock_ms=clock,
        ask=ask,
    )
    runner = CapacityRunner(controller)

    async def startup() -> None:
        runner.start()

    return (
        {
            **bindings,
            "throttle_registry": registry,
            "outcome_recorder": recorder,
            "partition_limiter": PartitionLimiter(registry),
            "capacity_runner": runner,
            "model_capacity_gate": ModelCapacityGate(model_ask or _no_model_decision),
        },
        startup,
    )


__all__ = [
    "CapacityRunner",
    "EngineCapacityPort",
    "cell_id_of",
    "model_capacity_ask",
    "with_capacity",
]
