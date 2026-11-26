"""The medulla capability guard (PIN-LAG: remove after the medulla re-pin).

Arbora pins released medulla. Medulla ``main`` serves engine methods and request
shapes the pinned release lacks. Every Arbora use of such a method goes through this
module, so a pinned release without it refuses ``UNAVAILABLE`` naming exactly the
method or shape that is missing, and nothing falls back to a local stand-in.

Guards held here (delete each with the re-pin that carries it):

- ``ApprovalGateResolve``: the approval gate question at plan mint.
- ``DeployRecordPut`` and ``DeployRecordGet``: the promotion deploy record.
- ``MutationOutbox`` pull and acknowledge: the approval and trust-flag consumers.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from arbora.api.invoke.pipeline import OperationRefused
from arbora.pin_lag.unavailable import PinLagUnavailable

__all__ = [
    "OUTBOX_MODELS",
    "PinLagUnavailable",
    "accepts_parameter",
    "refusal",
    "require_methods",
    "require_models",
]

#: The request shapes of the engine outbox pull and acknowledge.
OUTBOX_MODELS: tuple[str, ...] = ("MutationOutboxOpPull", "MutationOutboxOpAck")


def require_methods(*names: str) -> None:
    """Return once the pinned engine declares every method, else name the first missing."""
    from medulla.generated import METHOD_IDS

    for name in names:
        if name not in METHOD_IDS:
            raise PinLagUnavailable(name)


def require_models(names: Iterable[str]) -> None:
    """Return once the pinned engine declares every request shape, else name the first missing."""
    from medulla.generated import models

    for name in names:
        if not hasattr(models, name):
            raise PinLagUnavailable(name)


def accepts_parameter(method: Any, name: str) -> bool:
    """Whether the installed client method takes ``name`` (a request field the pin may lack).

    ``AuditAppend`` gained ``evidence`` on the engine's main branch; until the
    pin carries it the field is withheld and this answers ``False``.
    """
    import inspect

    return name in inspect.signature(method).parameters


def refusal(error: PinLagUnavailable) -> OperationRefused:
    """The served refusal of a missing capability: ``UNAVAILABLE`` naming it."""
    details: dict[str, Any] = {
        "reason": f"the installed engine does not serve {error.missing}",
        "missing": error.missing,
    }
    return OperationRefused("UNAVAILABLE", details)
