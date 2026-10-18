"""Canonical declarative host inputs, excluding opaque runtime resources."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from arbora.agent_host.start_admission import HostStartContext


@dataclass(frozen=True)
class HostStartConfiguration:
    """Actual arguments to the existing Arbora builder and listener.

    This is not an AU execution dictionary or an attestation of discovered
    workspace contents. Future construction must use these checked values.
    """

    workspace: str | None = None
    tag_prompts: dict[str, str] | None = None
    tag_env_vars: dict[str, str] | None = None
    graph_name: str = "GraphAgent"
    mcp_url: str | None = None
    mcp_config: str | None = None
    router_model: str | None = None
    agent_model: str | None = None
    min_confidence: float = 0.0
    host: str | None = "127.0.0.1"
    port: int | None = 9000
    name: str | None = None
    system_prompt: str | None = None
    enable_terminal_ui: bool = False
    enable_web_ui: bool | None = None
    enable_web_logs: bool = False

    def digest(self) -> str:
        # Validate before asdict: never deepcopy or serialize a caller object.
        for value in vars(self).values():
            _validate_value(value)
        values = asdict(self)
        if self.workspace is not None:
            values["workspace"] = str(Path(self.workspace).resolve(strict=True))
        encoded = json.dumps(
            {"schema": "arbora-host-start-v1", "inputs": values},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("invalid_launch_configuration")
        return hashlib.sha256(encoded).hexdigest()


def _validate_value(value: object) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is dict and len(value) <= 256:
        if all(type(key) is str and type(item) is str for key, item in value.items()):
            return
    raise ValueError("invalid_launch_configuration")


def check_start_configuration(
    context: HostStartContext | None,
    configuration: HostStartConfiguration,
    *,
    resources: tuple[object, ...],
    unused_arguments: dict[str, object],
) -> None:
    """Refuse unsupported resources or mismatched inputs before owner calls."""
    if context is None:
        return
    if any(resource is not None for resource in resources) or unused_arguments:
        raise PermissionError("configuration_unavailable")
    if configuration.digest() != context.request.configuration_digest:
        raise PermissionError("configuration_mismatch")


@dataclass(frozen=True, slots=True, repr=False, eq=False)
class HostRuntimeResources:
    """Exact borrowed resources; only the construction owner can attest them.

    Freezing this descriptor does not freeze an engine or execution dictionary.
    The owner must reject mutations or advance its attested configuration revision.
    """

    graph: object
    engine: object
    graph_config: object
    executor: object | None = None
    host_logs: object | None = None
    host_log_runtime: object | None = None

    def __post_init__(self) -> None:
        if self.graph is None or self.engine is None or self.graph_config is None:
            raise ValueError("runtime_resources_required")
        if self.host_logs is not None and self.host_log_runtime is not None:
            raise ValueError("one_host_log_composition_required")

    def same_resources(self, other: HostRuntimeResources | None) -> bool:
        return isinstance(other, HostRuntimeResources) and all(
            getattr(self, name) is getattr(other, name) for name in self.__slots__
        )
