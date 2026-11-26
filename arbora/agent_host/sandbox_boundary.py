"""The sandbox isolation boundary, declared from facts the kernel reports (ARBORA-OPS-R894).

The boundary is verified only when this very process reads, from ``/proc/self``, that it
runs with no effective capabilities, with ``no_new_privs`` set, under a seccomp filter,
and inside a user namespace that does not map the host's root. Each fact is read on
every call; nothing is cached, configured or taken from the environment, so a flag can
never declare a boundary that is not there. A fact that cannot be read counts as absent.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

HOST_ROOT_MAP = ("0", "0", "4294967295")


def _read_proc(name: str) -> str:
    return Path("/proc/self", name).read_text(encoding="utf-8")


def _status(text: str) -> Mapping[str, str]:
    fields: dict[str, str] = {}
    for line in text.splitlines():
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


@dataclass(frozen=True, slots=True)
class BoundaryEvidence:
    """What was read and whether it proves isolation."""

    verified: bool
    facts: Mapping[str, bool]

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, held in self.facts.items() if not held))


def verify_boundary(read: Callable[[str], str] = _read_proc) -> BoundaryEvidence:
    """Read the four facts; any unreadable or contrary fact leaves the boundary unverified."""

    def fact(check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except (OSError, ValueError):
            return False

    def status() -> Mapping[str, str]:
        return _status(read("status"))

    def user_namespace() -> bool:
        rows = [line.split() for line in read("uid_map").splitlines() if line.strip()]
        return bool(rows) and tuple(rows[0]) != HOST_ROOT_MAP

    facts = {
        "no_effective_capabilities": fact(lambda: int(status()["CapEff"], 16) == 0),
        "no_new_privs": fact(lambda: status()["NoNewPrivs"] == "1"),
        "seccomp_filter": fact(lambda: status()["Seccomp"] == "2"),
        "user_namespace": fact(user_namespace),
    }
    return BoundaryEvidence(all(facts.values()), facts)


def boundary_verified() -> bool:
    """``sandbox_verified`` for the process table: read afresh on every call."""
    return verify_boundary().verified
