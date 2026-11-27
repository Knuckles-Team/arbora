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
    "decide",
    "models",
    "scaffold_publication",
    "native",
    "fleet",
    "remote",
    "tool_client",
    "keepalive",
    "run_history",
    "caller",
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
    "events",
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


#: The shape of ``meristem.PUBLIC_HOST_MODULES`` on meristem ``main``. A pinned release
#: that does not declare the tuple is checked against this copy (PIN-LAG: remove it and
#: read the installed tuple only after the meristem re-pin).
MAIN_PUBLIC_HOST_MODULES: tuple[str, ...] = (
    "meristem.host_ports",
    "meristem.runtime",
    "meristem.graph",
    "meristem.rlm",
    "meristem.graph.hosted_run",
    "meristem.harness.plan_runtime",
    "meristem.harness.conversation_history",
)


def public_host_modules() -> tuple[str, ...]:
    """The installed ``meristem.PUBLIC_HOST_MODULES``, or refuse naming it."""
    try:
        import meristem
    except ImportError:
        raise PinLagUnavailable("meristem") from None
    declared = getattr(meristem, "PUBLIC_HOST_MODULES", None)
    if declared is None:
        raise PinLagUnavailable("meristem.PUBLIC_HOST_MODULES")
    return tuple(declared)


def fence_modules() -> tuple[tuple[str, ...], bool]:
    """The module list the import fence checks, and whether it came from the install.

    Against a pinned release without the declaration the fence still runs, on the main
    shape; the second value is ``False`` then.
    """
    try:
        return public_host_modules(), True
    except PinLagUnavailable:
        return MAIN_PUBLIC_HOST_MODULES, False


def hosted_run() -> Any:
    """``meristem.graph.hosted_run``, the harness release's module."""
    from meristem.graph import hosted_run as module

    return module


def apply_edits(edits: list[Any], root: Any, *, dry_run: bool = False) -> Any:
    """The harness edit engine: no public module exports it yet (gap filed)."""
    try:
        from meristem.harness.edit_engine import apply_edits as engine
    except ImportError:
        raise PinLagUnavailable("meristem.harness.edit_engine.apply_edits") from None
    return engine(edits, root, dry_run=dry_run)


def plan_types() -> SimpleNamespace:
    """``PlanItem`` and ``PlanState``: ``host_ports`` on main, ``harness.plan`` on the pin."""
    module = _host_ports_module()
    if hasattr(module, "PlanState"):
        return SimpleNamespace(PlanItem=module.PlanItem, PlanState=module.PlanState)
    from meristem.harness import plan

    return SimpleNamespace(PlanItem=plan.PlanItem, PlanState=plan.PlanState)


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


def require_task_fields(names: tuple[str, ...]) -> None:
    """``RunTask.<name>`` of the harness for each control step kind, or refuse naming it."""
    try:
        from meristem.graph import RunTask
    except ImportError:
        raise PinLagUnavailable("meristem.graph.RunTask") from None
    declared = {field.name for field in dataclasses.fields(RunTask)}
    for name in names:
        if name not in declared:
            raise PinLagUnavailable(f"meristem.graph.RunTask.{name}")


def emit_run_event(event: Any, binding: Any) -> None:
    """Send one run event through the bound ``HostPorts.emit_run_event`` (meristem main).

    ``binding`` is the host's ``HostPortsBinding`` or ``None`` before it binds, which
    sends nothing (the record's own contract: a no-op without a host). A pinned
    release refuses the record, so its refusal is raised, and a record without the
    method refuses naming it.
    """
    if binding is None:
        return
    if binding.refusal is not None:
        raise binding.refusal
    emit = getattr(binding.ports, "emit_run_event", None)
    if not callable(emit):
        raise PinLagUnavailable("meristem.host_ports.HostPorts.emit_run_event")
    emit(event)


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


def environment_declarations() -> tuple[Any, ...]:
    """The harness's public environment declarations, or refuse naming the export.

    ``MERISTEM-HARNESS-CYCLE-R058``: a pinned release without it leaves the host
    environment manifest unavailable; Arbora declares none of the harness's reads.
    """
    export = getattr(_host_ports_module(), "environment_declarations", None)
    if export is None:
        raise PinLagUnavailable("meristem.host_ports.environment_declarations")
    return tuple(export())
