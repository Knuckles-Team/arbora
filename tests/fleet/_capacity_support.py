"""Recorded capacity engine and a controlled clock for the controller tests."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from arbora.api.domain.fleet.error_budget import OutcomeClass, Partition
from arbora.fleet.capacity_proposals import CapacityCellView
from arbora.fleet.throttle_service import (
    ControllerConfig,
    OutcomeRecorder,
    ThrottleController,
    ThrottleRegistry,
)
from tests.fleet._support import CAPACITY_CONFIG, CAPACITY_PARTITION

PARTITION = CAPACITY_PARTITION
OTHER = Partition(
    tenant="t2", child="search", operation_class="read", policy_revision="p1"
)
CELL = CapacityCellView(
    cell_id="fleet:search",
    resource_class_iri="cell:search",
    unit="slots",
    capacity=10,
    min_capacity=1,
    max_capacity=10,
    measured_load=3,
    available_headroom=7,
    premise_revision=4,
)


class FakeClock:
    """A controlled clock in milliseconds; time moves only when the test says."""

    def __init__(self, now_ms: int = 1_000_000) -> None:
        self.now_ms = now_ms

    def __call__(self) -> int:
        return self.now_ms

    def advance(self, seconds: int) -> None:
        self.now_ms += seconds * 1000


class FakePort:
    """A recorded engine: one cell per partition, every call kept in order."""

    def __init__(self, cell: CapacityCellView = CELL) -> None:
        self.cells: dict[Partition, CapacityCellView] = {}
        self.default = cell
        self.calls: list[tuple[str, Any]] = []
        self.update_outcome = "accepted"

    async def cell(self, partition: Partition) -> CapacityCellView:
        self.calls.append(("cell", partition.child))
        return self.cells.get(partition, self.default)

    async def update_cell(self, cell: CapacityCellView, capacity: int) -> str:
        self.calls.append(("update_cell", capacity))
        if self.update_outcome == "accepted":
            self.default = replace(
                cell,
                capacity=capacity,
                max_capacity=capacity,
                premise_revision=cell.premise_revision + 1,
            )
        return self.update_outcome

    async def throttle_cell(
        self, cell: CapacityCellView, errors: int, requests: int, window_end_ms: int
    ) -> None:
        self.calls.append(("throttle_cell", (errors, requests, window_end_ms)))

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def fill(
    recorder: OutcomeRecorder,
    partition: Partition,
    *,
    ok: int = 0,
    bad: int = 0,
    caller: int = 0,
) -> None:
    for _ in range(ok):
        recorder.record(partition, OutcomeClass.SUCCESS)
    for _ in range(bad):
        recorder.record(partition, OutcomeClass.RETRYABLE_SERVICE_ERROR)
    for _ in range(caller):
        recorder.record(partition, OutcomeClass.PERMANENT_CALLER_ERROR)


def controller(
    port: FakePort,
    *,
    ask: Any = None,
    clock: FakeClock | None = None,
    registry: ThrottleRegistry | None = None,
    recorder: OutcomeRecorder | None = None,
) -> tuple[ThrottleController, ThrottleRegistry, OutcomeRecorder, FakeClock]:
    registry = registry or ThrottleRegistry()
    recorder = recorder or OutcomeRecorder()
    clock = clock or FakeClock()
    built = ThrottleController(
        registry=registry,
        recorder=recorder,
        port=port,
        config=ControllerConfig(aimd=CAPACITY_CONFIG),
        clock_ms=clock,
        ask=ask,
    )
    return built, registry, recorder, clock
