"""The names of the finance delegation grant (ARBORA-DATA-MARKET-R002).

An owner's delegation to the scheduler is one engine resource grant on the resource
``finance-delegation:<owner>``; these are the only names both the writer
(``finance.delegation.grant``) and the reader (the schedule host) share.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

__all__ = [
    "DELEGATION_PROTOCOL",
    "Delegation",
    "DelegatedSubjectAccess",
    "delegated",
    "delegation_grant_id",
    "delegation_holds",
    "delegation_resource",
]

#: The engine protocol the delegation is recorded under (finance data is graph data).
DELEGATION_PROTOCOL = "knowledge"


def delegation_resource(owner: str) -> str:
    return f"finance-delegation:{owner}"


def delegation_grant_id(owner: str, grant: str) -> str:
    return f"finance-delegation:{owner}:{grant}"


#: ``(method, params)`` to the engine, answers decoded.
Wire = Callable[[str, dict[str, Any]], Awaitable[Any]]


async def delegation_holds(wire: Wire, agent: str, owner: str, grant: str) -> bool:
    """Whether the engine's resource grant ledger lets ``agent`` act for ``owner``.

    The engine decides; no local grant stands in. A pinned release without
    ``CheckResourceAccess`` refuses ``UNAVAILABLE`` naming the method (PIN-LAG).
    """
    from arbora.pin_lag.medulla import guard

    guard("CheckResourceAccess")
    answer = await wire(
        "CheckResourceAccess",
        {
            "agent_id": agent,
            "resource_ref": delegation_resource(owner),
            "protocol": DELEGATION_PROTOCOL,
            "operation": grant,
        },
    )
    return isinstance(answer, Mapping) and answer.get("allow") is True


@dataclass(frozen=True, slots=True)
class Delegation:
    """What a scheduled fill asserts: the owner, the grant and the one account."""

    owner: str
    grant: str
    account: str


_HELD: ContextVar[Delegation | None] = ContextVar("finance_delegation", default=None)


@contextmanager
def delegated(owner: str, grant: str, account: str) -> Iterator[None]:
    """Assert, for one scheduled fill, that ``owner`` delegated ``grant`` over ``account``.

    The assertion is a claim, not an authority: the ownership check still asks the
    engine's grant ledger before it admits anything.
    """
    token = _HELD.set(Delegation(owner, grant, account))
    try:
        yield
    finally:
        _HELD.reset(token)


class DelegatedSubjectAccess:
    """The subject-access check of the service executor, with the delegated owner path.

    ``prepare_executor`` asks this one check for every service-executor operation. A human
    caller and an undelegated service caller fall to ``fallback`` unchanged. A service
    caller admitted for one account only when a delegation over exactly that account is
    asserted and the engine's grant ledger confirms it for that caller. There is no
    bypass: absent assertion, wrong account or a refused ledger answer is a refusal.
    """

    def __init__(
        self,
        wire: Wire,
        fallback: Callable[[Any, Any, str], Awaitable[bool]],
    ) -> None:
        self._wire = wire
        self._fallback = fallback

    async def __call__(self, client: Any, caller: Any, subject: str) -> bool:
        held = _HELD.get()
        if getattr(caller, "credential_kind", None) != "service" or held is None:
            return await self._fallback(client, caller, subject)
        if held.account != subject:
            return False
        return await delegation_holds(
            self._wire, str(caller.principal), held.owner, held.grant
        )
