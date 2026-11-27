"""A controlled construction owner for terminal-mode host tests (ARBORA-HOST-R009.2)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from tests.agent_host.test_runtime_binding import ConstructionOwner


@dataclass
class FakeSink:
    host_instance: str = "host-terminal-1"
    drains: int = 0

    async def drain(self) -> None:
        self.drains += 1


@dataclass(frozen=True)
class FakeLogRuntime:
    sink: FakeSink
    access: Any = None
    sessions: Any = None


def terminal_owner(
    tmp_path: Path, client: Any, log_runtime: FakeLogRuntime | None = None
) -> ConstructionOwner:
    owner = ConstructionOwner(tmp_path)
    owner.configuration = replace(
        owner.configuration, enable_terminal_ui=True, port=0, tag_prompts=None
    )
    owner.request = replace(
        owner.request,
        interfaces="terminal",
        configuration_digest=owner.configuration.digest(),
    )
    owner.grant = replace(owner.grant, request=owner.request)
    owner.resources = replace(
        owner.resources, terminal_client=client, host_log_runtime=log_runtime
    )
    owner.binding = replace(
        owner.binding,
        runtime=owner.resources,
        configuration_digest=owner.request.configuration_digest,
    )
    return owner


def launch_arguments(owner: ConstructionOwner) -> dict[str, Any]:
    return {
        **asdict(owner.configuration),
        "host_start": owner.context(),
        "host_runtime": owner.resources,
    }
