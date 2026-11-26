"""The one meristem capability guard (PIN-LAG: remove after the meristem re-pin).

Arbora pins released meristem, whose ``host_ports`` module is a set of ``register_*``
helpers. Meristem ``main`` replaces them with one ``HostPorts`` record set on every run
request, plus the answer types its ports return. Every Arbora binding of that record goes
through this module, so a pinned release without the symbol refuses ``UNAVAILABLE``
naming exactly what is missing, and nothing falls back to a local stand-in.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any

from arbora.pin_lag.unavailable import PinLagUnavailable

HOST_PORT_FIELDS: tuple[str, ...] = (
    "engine_decision",
    "decision",
    "approval",
    "confirm",
    "summary_provider",
    "study_content",
    "run_trace",
    "technique_measure",
    "sandbox_processes",
    "liveness_wait",
    "checkpoint_store",
    "changeset_submitter",
    "gate_runner",
)

_ANSWER_TYPES: tuple[str, ...] = (
    "EngineAnswer",
    "EngineDecisionRefused",
    "DecisionVerdict",
    "ApprovalOutcome",
    "ConfirmVerdict",
    "SummaryResult",
    "SummaryProviderDisabled",
)

MEDIA_METHODS: tuple[str, ...] = ("MediaPut", "MediaGet", "MediaTombstone")


def _host_ports_module() -> Any:
    try:
        from meristem import host_ports
    except ImportError:
        raise PinLagUnavailable("meristem.host_ports") from None
    return host_ports


def host_ports_type() -> Any:
    """The ``HostPorts`` record class, or refuse naming it."""
    cls = getattr(_host_ports_module(), "HostPorts", None)
    if cls is None or not dataclasses.is_dataclass(cls):
        raise PinLagUnavailable("meristem.host_ports.HostPorts")
    return cls


def port_types() -> SimpleNamespace:
    """The answer types the ports return, or refuse naming the first one missing."""
    module = _host_ports_module()
    found: dict[str, Any] = {}
    for name in _ANSWER_TYPES:
        value = getattr(module, name, None)
        if value is None:
            raise PinLagUnavailable(f"meristem.host_ports.{name}")
        found[name] = value
    return SimpleNamespace(**found)


def changeset_types() -> SimpleNamespace:
    """The changeset cycle's outcome types, or refuse naming the first one missing."""
    try:
        from meristem.harness import changeset_loop
    except ImportError:
        raise PinLagUnavailable("meristem.harness.changeset_loop") from None
    found: dict[str, Any] = {}
    for name in ("Submitted", "SourceConflict"):
        value = getattr(changeset_loop, name, None)
        if value is None:
            raise PinLagUnavailable(f"meristem.harness.changeset_loop.{name}")
        found[name] = value
    return SimpleNamespace(**found)


def require_fields(names: tuple[str, ...]) -> Any:
    """``HostPorts`` once every named field exists on it, else refuse the first missing."""
    cls = host_ports_type()
    declared = {field.name for field in dataclasses.fields(cls)}
    for name in names:
        if name not in declared:
            raise PinLagUnavailable(f"meristem.host_ports.HostPorts.{name}")
    return cls


def build_host_ports(**bound: Any) -> Any:
    """One ``HostPorts`` with each bound port set; an unknown port refuses."""
    for name in bound:
        if name not in HOST_PORT_FIELDS:
            raise ValueError(f"not a lane-bound host port: {name}")
    cls = require_fields(tuple(bound))
    return cls(**{name: value for name, value in bound.items() if value is not None})


def media_methods() -> tuple[str, ...]:
    """The engine media methods the served engine declares, or refuse naming the first missing."""
    try:
        from medulla.generated import METHOD_IDS
    except ImportError:
        raise PinLagUnavailable("medulla.generated.METHOD_IDS") from None
    for name in MEDIA_METHODS:
        if name not in METHOD_IDS:
            raise PinLagUnavailable(f"medulla.generated.METHOD_IDS.{name}")
    return MEDIA_METHODS


def assembly_model_input(result: Any) -> Any:
    """``AssemblyResult.model_input`` of the engine answer, or refuse naming it."""
    if not hasattr(result, "model_input"):
        raise PinLagUnavailable("medulla AssemblyResult.model_input")
    return result.model_input


def require_submit_field(name: str) -> None:
    """``SubmitWorkItemRequest.<name>`` of the engine, or refuse naming it."""
    try:
        from medulla.generated.models import SubmitWorkItemRequest
    except ImportError:
        raise PinLagUnavailable(
            "medulla.generated.models.SubmitWorkItemRequest"
        ) from None
    if name not in SubmitWorkItemRequest.model_fields:
        raise PinLagUnavailable(f"medulla SubmitWorkItemRequest.{name}")
