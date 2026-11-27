"""Run admission decides through the engine, never a local policy store (ARBORA-OPS-R050.3).

The engine holds the active policy that its ``DecisionLog`` ``policy_activation``
write activated. Arbora keeps no policy copy and no approval or exception store, so
admission asks the engine and acts on its answer. A refusal the engine names as a
policy denial refuses the admission and records nothing; an unreachable engine
refuses it too (fail closed).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from arbora.api.errors import EngineRefusal

__all__ = [
    "ENGINE_POLICY_DENIALS",
    "EngineRunPolicy",
    "RunPolicyDecision",
    "RunPolicyDenied",
    "RunPolicyUnavailable",
]

#: Engine refusals that mean "no verified approval, or beyond the active policy".
ENGINE_POLICY_DENIALS = frozenset(
    {
        "POLICY_REVIEW_MISSING",
        "POLICY_REVIEW_MISMATCH",
        "REVIEW_NOT_INDEPENDENT",
        "SCOPE_EXCEEDS_POLICY",
    }
)


class RunPolicyDenied(ValueError):
    """The engine's active policy refused the run."""

    def __init__(self, engine_code: str) -> None:
        self.engine_code = engine_code
        super().__init__(f"policy_denied: {engine_code}")


class RunPolicyUnavailable(ValueError):
    """The engine could not be asked, so the run is not admitted."""


class RunPolicyDecision(Protocol):
    """The one seam run admission decides through: the engine's active policy."""

    async def decide(self, subject: object, *, now: int) -> Any:
        """Return the engine's answer when it admits the run; raise when it denies."""


class EngineRunPolicy:
    """Asks the engine through one injected call and maps its refusals."""

    def __init__(self, ask: Callable[[object, int], Awaitable[Any]]) -> None:
        self._ask = ask

    async def decide(self, subject: object, *, now: int) -> Any:
        try:
            return await self._ask(subject, now)
        except EngineRefusal as refused:
            if refused.code in ENGINE_POLICY_DENIALS:
                raise RunPolicyDenied(refused.code) from refused
            raise
        except (OSError, TimeoutError) as unreachable:
            raise RunPolicyUnavailable("engine_unreachable") from unreachable
