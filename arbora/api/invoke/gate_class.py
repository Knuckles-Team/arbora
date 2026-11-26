"""The approval gate of a plan: class, approver roles and expiry (ARBORA-OPS-R918.2).

When the pipeline mints a ``plan_ref`` it asks the engine decision capability
which gate class the operation sits in, which roles may approve it and how long
the plan stays open. Arbora holds no class table. The engine's answer is read
through :data:`GateReader`; an operation the engine does not answer for, an
unreadable answer and a failed read all resolve to the strictest class and the
decide scope, so an unknown operation is never easier to approve than a known
one.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

#: The closed class set, strictest first.
GATE_CLASSES: tuple[str, ...] = (
    "legal_filing",
    "money_movement",
    "publishing",
    "production_qa",
)
STRICTEST_CLASS = GATE_CLASSES[0]
#: The scope and role every approver holds when the engine names no others.
DECIDE_SCOPE = "approvals:decide"
DEFAULT_APPROVER_ROLES: tuple[str, ...] = ("action-approvers",)
#: A plan the engine names no expiry for stays open for the plan lease span.
DEFAULT_EXPIRY_MS = 600_000
MAX_EXPIRY_MS = 24 * 60 * 60 * 1000

GateSource = Literal["engine", "default"]


@dataclass(frozen=True, slots=True)
class GateResolution:
    """Where one plan sits in the approval flow, stored on its plan lease."""

    gate_class: str
    approver_roles: tuple[str, ...]
    expires_in_ms: int
    source: GateSource

    def as_grant(self) -> dict[str, Any]:
        return {
            "gate_class": self.gate_class,
            "approver_roles": list(self.approver_roles),
            "gate_expires_in_ms": self.expires_in_ms,
            "gate_source": self.source,
            "decide_scope": DECIDE_SCOPE,
        }


#: ``reader(op_id, tenant)`` returns the engine's gate answer or ``None``.
GateReader = Callable[[str, str], Awaitable[Mapping[str, Any] | None]]

STRICT_DEFAULT = GateResolution(
    STRICTEST_CLASS, DEFAULT_APPROVER_ROLES, DEFAULT_EXPIRY_MS, "default"
)


async def no_engine_answer(_op_id: str, _tenant: str) -> Mapping[str, Any] | None:
    """The reader until the engine serves the gate question: no answer."""
    return None


#: ``send(method, params, tenant)`` reaches the engine as the service identity.
GateSend = Callable[[str, dict[str, Any], str], Awaitable[Any]]


def engine_gate_reader(send: GateSend) -> GateReader:
    """Ask the engine decision capability for one operation's gate (R918.2).

    The engine answers from the tenant's approval rules. ``unresolved`` and
    ``none`` give no gate class here, so the strict default applies: an operation
    is never easier to approve than the engine says. Arbora holds no class table.
    """

    async def read(op_id: str, tenant: str) -> Mapping[str, Any] | None:
        answer = await send(
            "ApprovalGateResolve", {"request": {"operation": op_id}}, tenant
        )
        if not isinstance(answer, Mapping) or answer.get("gate") not in {
            "single",
            "two_person",
        }:
            return None
        expiry = answer.get("expires_after_ms")
        return {
            "gate_class": answer.get("gate_class"),
            "approver_roles": answer.get("approver_roles"),
            "expires_in_ms": DEFAULT_EXPIRY_MS if expiry is None else expiry,
        }

    return read


def _parse(answer: Mapping[str, Any]) -> GateResolution | None:
    gate_class = answer.get("gate_class")
    roles = answer.get("approver_roles")
    expiry = answer.get("expires_in_ms")
    if gate_class not in GATE_CLASSES:
        return None
    if not isinstance(roles, list | tuple) or not roles:
        return None
    if not all(isinstance(role, str) and role for role in roles):
        return None
    if isinstance(expiry, bool) or not isinstance(expiry, int):
        return None
    if not 0 < expiry <= MAX_EXPIRY_MS:
        return None
    return GateResolution(str(gate_class), tuple(roles), expiry, "engine")


async def resolve_gate(reader: GateReader, op_id: str, tenant: str) -> GateResolution:
    """The engine's gate for ``op_id``, or the strict default.

    A malformed answer and a failed read fail closed to the default; neither
    raises, so a plan is never minted without a gate.
    """
    try:
        answer = await reader(op_id, tenant)
    except Exception:  # noqa: BLE001 - an unreadable answer is no answer
        return STRICT_DEFAULT
    if not isinstance(answer, Mapping):
        return STRICT_DEFAULT
    return _parse(answer) or STRICT_DEFAULT
