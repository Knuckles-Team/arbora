"""The sandbox fleet profile: a closed, declared set of local connectors.

The profile applies only when the operator selects the sandbox
(ARBORA-FLEET-R085). It is read-only data in one declarative file. A
connector name is data here, never a branch in code. The fixture-serving
mode that makes each connector run without credentials belongs to the
connector SDK; this module implements none of it.
"""

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROFILE_PATH = Path(__file__).with_name("sandbox_fleet.json")
PROFILE_VERSION = 1

#: Failure keys the onboarding pass uses for shared infrastructure, not for one
#: connector. They always fail the run, in every mode.
INFRASTRUCTURE_KEYS = frozenset({"child-auth", "registry"})


class SandboxProfileError(ValueError):
    """The profile file is missing, malformed or not the supported version."""


@dataclass(frozen=True, slots=True)
class SandboxFleetProfile:
    """The closed set of connectors the sandbox declares."""

    connectors: frozenset[str]


def load_sandbox_profile(path: Path | None = None) -> SandboxFleetProfile:
    """Read and validate the profile; a malformed file refuses, never defaults."""
    target = path or PROFILE_PATH
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SandboxProfileError("sandbox fleet profile unreadable") from exc
    if not isinstance(raw, dict) or raw.get("version") != PROFILE_VERSION:
        raise SandboxProfileError("sandbox fleet profile version unsupported")
    names = raw.get("connectors")
    if (
        not isinstance(names, list)
        or not names
        or not all(isinstance(n, str) and n for n in names)
        or len(set(names)) != len(names)
    ):
        raise SandboxProfileError("sandbox fleet profile needs a closed name list")
    return SandboxFleetProfile(connectors=frozenset(names))


def select_endpoints[T](
    endpoints: Collection[T], profile: SandboxFleetProfile
) -> tuple[T, ...]:
    """Keep only the declared connectors, in the order given."""
    return tuple(e for e in endpoints if getattr(e, "name", None) in profile.connectors)


def sandbox_ok(failed: Mapping[str, str], profile: SandboxFleetProfile) -> bool:
    """True when no declared connector and no shared infrastructure step failed.

    A failure outside the declared set stays in the report and does not fail
    the run (ARBORA-FLEET-R085.1).
    """
    return not any(
        name in profile.connectors or name in INFRASTRUCTURE_KEYS for name in failed
    )


def onboarding_ok(
    failed: Mapping[str, str], profile: SandboxFleetProfile | None
) -> bool:
    """The one exit rule: any failure fails a production run; a sandbox run follows the profile."""
    return not failed if profile is None else sandbox_ok(failed, profile)
