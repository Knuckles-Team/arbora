"""Fleet acceptance: four checks per connector, reasoned skips only (ARBORA-RELEASE-R003.9).

For every configured connector :func:`check_fleet` runs

* (a) ``fleet.status`` reports the child healthy;
* (b) the live tool count equals the tool count of the engine-held pack record;
* (c) one read-only tool call succeeds through the ``fleet.call`` pipeline;
* (d) the engine pack digest equals the connector's reported pack digest.

Each check answers ``PASS`` (``CHECK_OK``), ``FAIL``, ``GAP`` (a fact could not be read) or
``SKIP``. A skip needs a reason in ``acceptance.expected_skip`` of the fleet
configuration; a skip without one is a ``GAP``. ``GAP`` never passes: any
``FAIL`` or ``GAP`` makes the receipt ``blocked``. Arbora holds no pack record;
every fact is read from the fleet, the engine or the connector.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

CHECK_OK = "PASS"
FAIL = "FAIL"
GAP = "GAP"
SKIP = "SKIP"
CHECKS: tuple[str, ...] = ("health", "tool_count", "read_call", "pack_digest")
HEALTHY_STATE = "up"
HEALTHY_BREAKER = "closed"


class AcceptancePorts(Protocol):
    """What the checks read; each answers ``None`` when the fact cannot be read."""

    async def child(self, connector: str) -> Mapping[str, Any] | None:
        """The ``fleet.status`` child row: ``state``, ``breaker``, ``mounted_tools``."""

    async def pack(self, connector: str) -> Mapping[str, Any] | None:
        """The engine pack record: ``tool_count`` and ``pack_digest``."""

    async def read_call(self, connector: str) -> bool | None:
        """Whether one read-only call succeeded through ``fleet.call``."""

    async def digest_matches(self, connector: str) -> bool | None:
        """Whether the engine pack digest equals the digest of the connector's own pack."""


@dataclass(frozen=True)
class ConnectorResult:
    connector: str
    results: Mapping[str, str]
    cause: Mapping[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector": self.connector,
            "results": dict(self.results),
            "cause": dict(self.cause),
        }


def _health(child: Mapping[str, Any] | None) -> tuple[str, str]:
    if child is None:
        return GAP, "no child row in fleet.status"
    state, breaker = child.get("state"), child.get("breaker")
    if state == HEALTHY_STATE and breaker in (None, HEALTHY_BREAKER):
        return CHECK_OK, ""
    return FAIL, f"child state {state}, breaker {breaker}"


def _tool_count(
    child: Mapping[str, Any] | None, pack: Mapping[str, Any] | None
) -> tuple[str, str]:
    live = None if child is None else child.get("mounted_tools")
    held = None if pack is None else pack.get("tool_count")
    if not isinstance(live, int) or not isinstance(held, int):
        return GAP, "the live or the engine-held tool count is unavailable"
    if live == held:
        return CHECK_OK, ""
    return FAIL, f"live {live}, engine pack {held}"


def _digest(matches: bool | None) -> tuple[str, str]:
    if matches is None:
        return GAP, "the engine or the connector pack digest is unavailable"
    if matches:
        return CHECK_OK, ""
    return FAIL, "the engine pack digest differs from the connector's"


async def check_connector(
    ports: AcceptancePorts, connector: str, skip_reason: str | None
) -> ConnectorResult:
    """Run the four checks for one connector."""
    if skip_reason is not None:
        if not skip_reason.strip():
            return ConnectorResult(
                connector,
                {name: GAP for name in CHECKS},
                {"skip": "a skip needs a reason"},
            )
        return ConnectorResult(
            connector,
            {name: SKIP for name in CHECKS},
            {"skip": skip_reason},
        )
    child = await ports.child(connector)
    pack = await ports.pack(connector)
    called = await ports.read_call(connector)
    matches = await ports.digest_matches(connector)
    health = _health(child)
    count = _tool_count(child, pack)
    digest = _digest(matches)
    if called is None:
        read = (GAP, "no read-only probe call is configured or reachable")
    else:
        read = (CHECK_OK, "") if called else (FAIL, "the read-only call failed")
    outcomes = dict(zip(CHECKS, (health, count, read, digest), strict=True))
    return ConnectorResult(
        connector,
        {name: result for name, (result, _) in outcomes.items()},
        {name: cause for name, (result, cause) in outcomes.items() if cause},
    )


async def check_fleet(
    ports: AcceptancePorts,
    connectors: Sequence[str],
    expected_skip: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The acceptance receipt: ``passed``, or ``blocked`` on any ``FAIL`` or ``GAP``."""
    skips = dict(expected_skip or {})
    rows = [
        await check_connector(ports, name, skips.get(name))
        for name in sorted(connectors)
    ]
    blocked = any(
        result in (FAIL, GAP) for row in rows for result in row.results.values()
    )
    return {
        "state": "blocked" if blocked else "passed",
        "connectors": [row.as_dict() for row in rows],
    }


__all__ = [
    "CHECKS",
    "FAIL",
    "GAP",
    "CHECK_OK",
    "SKIP",
    "AcceptancePorts",
    "ConnectorResult",
    "check_connector",
    "check_fleet",
]
