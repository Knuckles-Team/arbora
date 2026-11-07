"""The host's owned run-time state (ARBORA-HOST-R039).

``HostContext`` is built once by the composition root and passed by argument,
narrowest field first. A field is one owned object that holds its own lock and
is never reassigned. There is deliberately no module-level accessor: that
would be the global the context replaces.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import OrchestratorA2ARouter
from arbora.a2a.service import A2AService
from arbora.run_origin import RunOriginRegistry

__all__ = ["HostContext", "RunState", "build_host_context"]


@dataclass(frozen=True, slots=True)
class RunState:
    """Live agent runs: their origin registry and the A2A service over them."""

    origins: RunOriginRegistry
    a2a: A2AService


@dataclass(frozen=True, slots=True)
class HostContext:
    """Every owned run-time object of one host process."""

    runs: RunState


def build_host_context(engine_provider: Callable[[], Any]) -> HostContext:
    """Build the host state once; ``engine_provider`` resolves the engine lazily."""
    origins = RunOriginRegistry()
    a2a = A2AService(
        authority=WorkItemA2AAuthority(engine_provider, origins=origins),
        router=OrchestratorA2ARouter(engine_provider),
    )
    return HostContext(runs=RunState(origins=origins, a2a=a2a))
