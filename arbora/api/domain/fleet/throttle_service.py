"""In-process read/config facade over the pure AIMD controller state.

Part of ARBORA-CAPACITY-R002 and the ARBORA-CAPACITY-R001 build sequence:
holds the latest recorded
:class:`~arbora.api.domain.fleet.error_budget.ThrottleDecision` and the selected
:class:`~arbora.api.domain.fleet.error_budget.ThrottleMode` per partition, so a
hosted operation (``capacity.throttle.status`` / ``capacity.throttle.
set_mode`` in :mod:`arbora.api.ops.capacity`) can read or administer
controller state without a second fleet call path or a cloned engine
capacity model.

The registry never calls a child or the engine itself: it only stores and
reports what it is given. A partition with no recorded decision yet reports
an explicit ``no_decision_recorded`` reason and no limit, never a fabricated
healthy status. The outcome recorder (ARBORA-CAPACITY-R001.4), the controller
that closes windows, the admission limiter at fleet forward
(ARBORA-CAPACITY-R001.5) and the per-model gate (ARBORA-CAPACITY-R752) live
beside it and share its state.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from arbora.api.domain.fleet.capacity_proposals import (
    CapacityAppliedReceipt,
    CapacityPort,
    CapacityProposalError,
    CapacityVerdict,
    Judge,
    ReplicaIntents,
    VerdictKind,
    apply,
    build_proposal,
    judge,
    read_back,
)
from arbora.api.domain.fleet.error_budget import (
    AimdConfig,
    BudgetWindow,
    OutcomeClass,
    OutcomeSample,
    Partition,
    ThrottleDecision,
    ThrottleMode,
    decide,
)

logger = logging.getLogger(__name__)

CAPACITY_THROTTLED = "CAPACITY_THROTTLED"
FALLBACK_MODEL_LIMIT = 1

_NO_DECISION_REASON = "no_decision_recorded"


@dataclass(frozen=True, slots=True)
class ThrottleStatus:
    """A caller-visible snapshot of one partition's throttle state."""

    partition: Partition
    mode: ThrottleMode
    current_limit: int | None
    reason: str
    last_decision: ThrottleDecision | None


class ThrottleRegistry:
    """Bounded in-memory per-partition throttle state.

    Not durable and not an authority on its own: callers reach it only
    through the hosted capacity operations (scope- and audit-checked by the
    shared invoke pipeline) or a bound limiter, never directly.
    """

    def __init__(self, *, default_mode: ThrottleMode = ThrottleMode.OBSERVE) -> None:
        self._default_mode = default_mode
        self._modes: dict[tuple[str, str, str, str], ThrottleMode] = {}
        self._decisions: dict[tuple[str, str, str, str], ThrottleDecision] = {}
        self._by_window: dict[str, ThrottleDecision] = {}
        self._receipts: dict[tuple[str, str, str, str], CapacityAppliedReceipt] = {}
        self._would_refuse: dict[tuple[str, str, str, str], int] = {}

    def record_decision(self, decision: ThrottleDecision) -> None:
        """Store the latest decision for its partition, replacing any prior one."""
        self._decisions[decision.partition.key()] = decision
        self._by_window[decision.window_digest] = decision

    def decision_for(self, window_digest: str) -> ThrottleDecision | None:
        """The stored decision of a window, so a replayed window changes nothing."""
        return self._by_window.get(window_digest)

    def record_receipt(
        self, partition: Partition, receipt: CapacityAppliedReceipt
    ) -> None:
        self._receipts[partition.key()] = receipt

    def latest_receipt(self, partition: Partition) -> CapacityAppliedReceipt | None:
        return self._receipts.get(partition.key())

    def note_would_refuse(self, partition: Partition) -> int:
        """Count a call enforce mode would have refused; returns the new count."""
        key = partition.key()
        self._would_refuse[key] = self._would_refuse.get(key, 0) + 1
        return self._would_refuse[key]

    def would_refuse_count(self, partition: Partition) -> int:
        return self._would_refuse.get(partition.key(), 0)

    def set_mode(self, partition: Partition, mode: ThrottleMode) -> None:
        """Select observe or enforce mode for one partition."""
        self._modes[partition.key()] = mode

    def mode(self, partition: Partition) -> ThrottleMode:
        return self._modes.get(partition.key(), self._default_mode)

    def status(self, partition: Partition) -> ThrottleStatus:
        decision = self._decisions.get(partition.key())
        mode = self.mode(partition)
        if decision is None:
            return ThrottleStatus(
                partition=partition,
                mode=mode,
                current_limit=None,
                reason=_NO_DECISION_REASON,
                last_decision=None,
            )
        return ThrottleStatus(
            partition=partition,
            mode=mode,
            current_limit=decision.new_limit,
            reason=decision.reason,
            last_decision=decision,
        )


class OutcomeRecorder:
    """Bounded per-partition outcome samples for the open budget window.

    Caller errors and policy denials are recorded (the window digest covers
    them) but stay ineligible for the error fraction (``error_budget``).
    """

    def __init__(self, *, max_samples: int = 10_000) -> None:
        self._max = max(max_samples, 1)
        self._open: dict[Partition, list[OutcomeSample]] = {}

    def record(self, partition: Partition, outcome: OutcomeClass) -> None:
        samples = self._open.setdefault(partition, [])
        samples.append(OutcomeSample(outcome))
        del samples[: max(len(samples) - self._max, 0)]

    def partitions(self) -> tuple[Partition, ...]:
        return tuple(sorted(self._open, key=Partition.key))

    def close(self, partition: Partition, window_id: str) -> BudgetWindow:
        samples = tuple(self._open.pop(partition, ()))
        return BudgetWindow(window_id=window_id, partition=partition, samples=samples)


_POLICY_CODES = frozenset(
    {"SCOPE_REQUIRED", "POLICY_DENIED", "ACCESS_DENIED", "SUBJECT_ACCESS_DENIED"}
)
_CALLER_CODES = frozenset({"INVALID_ARGUMENT", "UNKNOWN_TOOL", "NOT_FOUND"})


def classify_outcome(error: BaseException | None) -> OutcomeClass:
    """Classify one governed call: only a child's own failure is eligible."""
    if error is None:
        return OutcomeClass.SUCCESS
    code = str(getattr(error, "code", ""))
    if code in _POLICY_CODES:
        return OutcomeClass.POLICY_DENIAL
    if code in _CALLER_CODES:
        return OutcomeClass.PERMANENT_CALLER_ERROR
    if isinstance(error, TimeoutError) or code == "TIMEOUT":
        return OutcomeClass.TIMEOUT
    return OutcomeClass.RETRYABLE_SERVICE_ERROR


class CapacityThrottled(Exception):
    """An enforced partition is at its limit (HTTP 429, ``Retry-After: 1``)."""

    code = CAPACITY_THROTTLED

    details: Mapping[str, Any]

    @classmethod
    def for_partition(cls, partition: Partition) -> CapacityThrottled:
        refusal = cls(CAPACITY_THROTTLED)
        refusal.details = {"child": partition.child, "retry_after_s": 1}
        return refusal


class PartitionLimiter:
    """Admission at fleet forward: at most ``current_limit`` calls in flight.

    ``observe`` admits every call and counts the ones ``enforce`` would refuse.
    A partition with no decision has no limit. The first refusal of a window
    reaches ``on_first_refusal`` (the audit event of R001.5).
    """

    def __init__(
        self,
        registry: ThrottleRegistry,
        *,
        on_first_refusal: Callable[[Partition], None] | None = None,
    ) -> None:
        self._registry = registry
        self._in_flight: dict[Partition, int] = {}
        self._on_first = on_first_refusal

    def in_flight(self, partition: Partition) -> int:
        return self._in_flight.get(partition, 0)

    def _over_limit(self, partition: Partition) -> bool:
        limit = self._registry.status(partition).current_limit
        return limit is not None and self.in_flight(partition) >= limit

    def _refuse(self, partition: Partition) -> None:
        if self._registry.note_would_refuse(partition) == 1 and self._on_first:
            self._on_first(partition)

    @contextlib.asynccontextmanager
    async def acquire(self, partition: Partition) -> AsyncIterator[None]:
        if self._over_limit(partition):
            self._refuse(partition)
            if self._registry.mode(partition) is ThrottleMode.ENFORCE:
                raise CapacityThrottled.for_partition(partition)
        self._in_flight[partition] = self.in_flight(partition) + 1
        try:
            yield
        finally:
            self._in_flight[partition] -= 1


class CapacityGuard:
    """Admit one forwarded call, then record what the child did with it.

    A call refused at the limiter is not an outcome of the child: it is raised
    as ``refusal(throttled)`` and never recorded.
    """

    def __init__(
        self,
        limiter: PartitionLimiter,
        recorder: OutcomeRecorder,
        refusal: Callable[[CapacityThrottled], Exception],
    ) -> None:
        self._limiter = limiter
        self._recorder = recorder
        self._refusal = refusal

    async def run(
        self,
        partition_of: Callable[[], Partition],
        forward: Callable[[], Awaitable[Any]],
    ) -> Any:
        partition = partition_of()
        try:
            async with self._limiter.acquire(partition):
                result = await self._forward(partition, forward)
        except CapacityThrottled as throttled:
            raise self._refusal(throttled) from throttled
        self._recorder.record(partition, classify_outcome(None))
        return result

    async def _forward(
        self, partition: Partition, forward: Callable[[], Awaitable[Any]]
    ) -> Any:
        try:
            return await forward()
        except Exception as exc:
            self._recorder.record(partition, classify_outcome(exc))
            raise


class _Unguarded:
    """No capacity controller is bound: forward the call unchanged."""

    async def run(
        self,
        partition_of: Callable[[], Partition],
        forward: Callable[[], Awaitable[Any]],
    ) -> Any:
        return await forward()


UNGUARDED = _Unguarded()


@dataclass(frozen=True, slots=True)
class ControllerConfig:
    """``capacity.aimd`` of ``~/.config/arbora/config.json`` with its bounds."""

    aimd: AimdConfig
    window_seconds: int = 60
    policy_revision: int = 1

    def __post_init__(self) -> None:
        if not 10 <= self.window_seconds <= 600:
            raise ValueError("window_seconds must be in [10, 600]")
        if not 1 <= self.aimd.alpha <= 64:
            raise ValueError("alpha must be in [1, 64]")
        if not 0.1 <= self.aimd.beta <= 0.9:
            raise ValueError("beta must be in [0.1, 0.9]")
        if not 0 <= self.aimd.floor <= 100:
            raise ValueError("floor must be in [0, 100]")
        if not 1 <= self.aimd.min_sample_count <= 10_000:
            raise ValueError("min_sample_count must be in [1, 10000]")
        if not 0.01 <= self.aimd.error_budget_fraction <= 0.5:
            raise ValueError("error_budget_fraction must be in [0.01, 0.5]")

    @classmethod
    def from_section(cls, section: Mapping[str, Any]) -> ControllerConfig:
        """Read ``capacity.aimd``; an absent key takes the documented default."""
        aimd = AimdConfig(
            version=str(section.get("version", "aimd-1")),
            alpha=int(section.get("alpha", 1)),
            beta=float(section.get("beta", 0.5)),
            floor=int(section.get("floor", 1)),
            min_sample_count=int(section.get("min_sample_count", 20)),
            error_budget_fraction=float(section.get("error_budget_fraction", 0.1)),
        )
        return cls(
            aimd=aimd,
            window_seconds=int(section.get("window_seconds", 60)),
            policy_revision=int(section.get("policy_revision", 1)),
        )


class ThrottleController:
    """Closes each partition's window, decides, and runs the raise path.

    A narrowing decision needs no engine call (in enforce mode the window is
    reported through ``throttle_cell``). A raise waits for an engine ``allow``
    (R751): otherwise the prior limit is kept and the reason says why.
    """

    def __init__(
        self,
        *,
        registry: ThrottleRegistry,
        recorder: OutcomeRecorder,
        port: CapacityPort,
        config: ControllerConfig,
        clock_ms: Callable[[], int],
        ask: Judge | None = None,
        intents: ReplicaIntents | None = None,
    ) -> None:
        self._registry = registry
        self._recorder = recorder
        self._port = port
        self._config = config
        self._clock_ms = clock_ms
        self._ask = ask
        self._intents = intents
        self._last_ms = clock_ms()

    async def tick(self) -> list[ThrottleDecision]:
        """Close every open window at the controller's clock."""
        end = self._clock_ms()
        start, self._last_ms = self._last_ms, end
        decisions = []
        for partition in self._recorder.partitions():
            window = self._recorder.close(partition, f"w{end}")
            decisions.append(await self.process_window(window, start, end))
        return decisions

    async def process_window(
        self, window: BudgetWindow, start_ms: int, end_ms: int
    ) -> ThrottleDecision:
        """Decide one closed window once; a replay returns the stored decision."""
        stored = self._registry.decision_for(window.digest())
        if stored is not None:
            return stored
        partition = window.partition
        cell = await self._port.cell(partition)
        prior = self._registry.status(partition).current_limit
        prior = cell.capacity if prior is None else prior
        decision = decide(
            window=window,
            prior_limit=prior,
            engine_headroom=cell.capacity,
            config=self._config.aimd,
            mode=self._registry.mode(partition),
        )
        decision = await self._settle(decision, cell, start_ms, end_ms, window)
        self._registry.record_decision(decision)
        return decision

    async def _settle(
        self,
        decision: ThrottleDecision,
        cell: Any,
        start_ms: int,
        end_ms: int,
        window: BudgetWindow,
    ) -> ThrottleDecision:
        if decision.new_limit < decision.prior_limit:
            if decision.mode is ThrottleMode.ENFORCE:
                await self._port.throttle_cell(
                    cell,
                    window.eligible_failure_count(),
                    window.eligible_count(),
                    end_ms,
                )
            return decision
        if decision.new_limit == decision.prior_limit:
            return decision
        return await self._raise(decision, cell, start_ms, end_ms)

    async def _raise(
        self, decision: ThrottleDecision, cell: Any, start_ms: int, end_ms: int
    ) -> ThrottleDecision:
        partition = decision.partition
        try:
            proposal = build_proposal(
                decision,
                cell,
                window_start_ms=start_ms,
                window_end_ms=end_ms,
                policy_revision=self._config.policy_revision,
            )
        except CapacityProposalError as exc:
            return replace(decision, new_limit=decision.prior_limit, reason=exc.code)
        if proposal is None:
            return decision
        verdict = await self._verdict(proposal)
        applied = await apply(
            self._port,
            proposal,
            verdict,
            mode=decision.mode,
            cell=cell,
            intents=self._intents,
        )
        held = applied.outcome == "held"
        limit_now = decision.prior_limit if held else decision.new_limit
        receipt = await read_back(
            self._port, partition, proposal, verdict, applied, limit_now=limit_now
        )
        self._registry.record_receipt(partition, receipt)
        if held:
            return replace(
                decision,
                new_limit=decision.prior_limit,
                reason=f"raise_held:{applied.code}",
            )
        return decision

    async def _verdict(self, proposal: Any) -> CapacityVerdict:
        if self._ask is None:
            return CapacityVerdict(
                VerdictKind.UNAVAILABLE,
                "VERDICT_UNAVAILABLE",
                proposal.proposal_digest,
                proposal.policy_revision,
                proposal.premise_revision,
            )
        return await judge(self._ask, proposal)

    async def run(
        self,
        sleep: Callable[[float], Awaitable[None]],
        keep_going: Callable[[], bool],
    ) -> None:
        """The background loop: wait one window, close, repeat while told to."""
        while keep_going():
            await sleep(self._config.window_seconds)
            await self.tick()


@dataclass(frozen=True, slots=True)
class ModelCapacity:
    """The engine's answer for one model endpoint."""

    limit: int
    breaker: str
    group_slots: int


class ModelCapacityGate:
    """Per-model concurrency from the engine decision (ARBORA-CAPACITY-R752).

    ``ask`` returns the engine's ``CapacityAnswer``: ``effective_limit`` (the
    limit already narrowed by the breaker), ``breaker`` and ``granted`` group
    slots. Arbora holds no AIMD constants for models: with no decision, a
    failed ask or a malformed answer the limit is ``FALLBACK_MODEL_LIMIT``.
    """

    def __init__(
        self, ask: Callable[[str], Awaitable[Mapping[str, Any] | None]]
    ) -> None:
        self._ask = ask

    async def limit_for(self, endpoint: str) -> ModelCapacity:
        try:
            answer = await self._ask(endpoint)
        except Exception:
            logger.warning("limit_for failed", exc_info=True)
            return _FALLBACK_MODEL
        if not isinstance(answer, Mapping):
            return _FALLBACK_MODEL
        limit, slots = answer.get("effective_limit"), answer.get("granted")
        breaker = answer.get("breaker")
        valid = (
            type(limit) is int
            and type(slots) is int
            and limit >= 0
            and slots >= 0
            and breaker in {"closed", "half_open", "open"}
        )
        if not valid:
            return _FALLBACK_MODEL
        return ModelCapacity(int(limit), str(breaker), int(slots))


_FALLBACK_MODEL = ModelCapacity(FALLBACK_MODEL_LIMIT, "closed", FALLBACK_MODEL_LIMIT)


__all__ = [
    "CAPACITY_THROTTLED",
    "FALLBACK_MODEL_LIMIT",
    "UNGUARDED",
    "CapacityGuard",
    "CapacityThrottled",
    "ControllerConfig",
    "ModelCapacity",
    "ModelCapacityGate",
    "OutcomeRecorder",
    "PartitionLimiter",
    "ThrottleController",
    "ThrottleRegistry",
    "ThrottleStatus",
    "classify_outcome",
]
