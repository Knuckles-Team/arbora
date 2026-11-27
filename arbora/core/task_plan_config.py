"""The trusted task-plan configuration shape (ARBORA-HOST-R038.6).

Lives in core so ``core.settings`` validates ``ARBORA_TASK_PLAN`` without importing
the invoke pipeline or the MCP server; the runtime in ``mcp_server.task_plan`` imports
this class.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from arbora.core.claims import freeze_claims

__all__ = ["TaskPlanConfig"]


@dataclass(frozen=True)
class TaskPlanConfig:
    """Trusted composition settings, never fields of a model proposal."""

    # The schema is published per tenant at request time.  Keep the field for
    # positional/config compatibility, but an operator may omit it entirely.
    feature_schema: Mapping[str, Any] = field(default_factory=dict)
    policy: Mapping[str, Any] = field(default_factory=dict)
    max_concurrency: int = 0
    max_steps: int = 0
    request_limit: int = 50
    total_tokens_limit: int = 100_000
    timeout_seconds: float = 600.0
    plan_scaffold: Literal["on", "off"] = "off"

    def __post_init__(self) -> None:
        if type(self.plan_scaffold) is not str or self.plan_scaffold not in (
            "on",
            "off",
        ):
            raise ValueError("plan_scaffold must be on or off")
        if not isinstance(self.feature_schema, Mapping) or not isinstance(
            self.policy, Mapping
        ):
            raise ValueError("task plan schema and policy must be mappings")
        if type(self.max_concurrency) is not int or not 1 <= self.max_concurrency <= 8:
            raise ValueError("max_concurrency must be between 1 and 8")
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= 64:
            raise ValueError("max_steps must be between 1 and 64")
        _validate_budgets(
            self.request_limit, self.total_tokens_limit, self.timeout_seconds
        )
        object.__setattr__(self, "feature_schema", freeze_claims(self.feature_schema))
        object.__setattr__(self, "policy", freeze_claims(self.policy))


def _validate_budgets(
    request_limit: int, total_tokens_limit: int, timeout_seconds: float
) -> None:
    """Reject non-integral budgets and non-finite deadlines before composition."""
    import math

    for value in (request_limit, total_tokens_limit):
        if type(value) is not int or value < 1:
            raise ValueError("task plan budgets must be positive integers")
    if (
        type(timeout_seconds) not in (int, float)
        or not math.isfinite(timeout_seconds)
        or timeout_seconds <= 0
    ):
        raise ValueError("task plan timeout must be finite and positive")
