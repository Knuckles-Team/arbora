"""The invoke services, the fleet-call decision and the external-effect registry (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from arbora.api.domain.fleet.admission import AdmissionGate
from arbora.api.invoke.audit import EffectJournal
from arbora.api.invoke.autonomy import AskVerdict
from arbora.api.invoke.executor import (
    OperationRuntime,
)
from arbora.api.invoke.gate_class import GateReader, no_engine_answer
from arbora.api.invoke.hooks import HookChain
from arbora.api.invoke.plan import PlanStore
from arbora.api.invoke.steps import (
    RequestCaller,
    SchemaValidate,
)
from arbora.api.policy.eunomia import PolicyGate
from arbora.api.registry import (
    AuditClass,
    Confirm,
    Effect,
    EgSchemaRef,
    Executor,
    Idempotency,
    PrincipalRule,
    Registry,
)
from arbora.core.settings import RuntimeConfig

_logger = logging.getLogger(__name__)


AuditWrite = Callable[[Mapping[str, str], AuditClass, RequestCaller], Awaitable[None]]


AuditPreflight = Callable[
    [Mapping[str, str], AuditClass, RequestCaller], Awaitable[str]
]


@dataclass(frozen=True, slots=True)
class FleetCallDecision:
    """Trusted live catalog authority for one exact child call."""

    effect: Effect
    confirm: Confirm
    principals: PrincipalRule
    executor: Executor
    required_scopes: frozenset[str]
    executor_scopes: frozenset[str]
    subject_id: str | None
    credential_mode: str
    #: The tool's resource declaration (``SCION-CONNECTOR-CONTROL-R911``), when it has one.
    resource_protocol: str | None = None
    resource_param: str | None = None


FleetEffect = Callable[
    [Any, Mapping[str, Any], RequestCaller],
    Awaitable[FleetCallDecision],
]


_RUNTIME_PORTS = (
    "verify_current",
    "as_caller",
    "as_service",
    "check_subject_access",
    "dispatch",
)


_PLAN_PORTS = ("issue", "validate", "consume")


_PLAN_GATED_CONFIRMS = frozenset({Confirm.PLAN, Confirm.CONSOLE})


def _has_methods(owner: Any, names: tuple[str, ...]) -> bool:
    return all(callable(getattr(owner, name, None)) for name in names)


def _valid_key(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 256


def _declared_owners(modules: Iterable[str] | None = None) -> dict[str, str]:
    """Every ops module's ``NATIVE_IDEMPOTENCY``: op id -> replay owner."""
    import importlib
    import pkgutil

    import arbora.api.ops as ops_package

    names = (
        modules
        if modules is not None
        else [
            f"{ops_package.__name__}.{info.name}"
            for info in pkgutil.iter_modules(ops_package.__path__)
        ]
    )
    owners: dict[str, str] = {}
    for name in names:
        module = importlib.import_module(name)
        owners.update(getattr(module, "NATIVE_IDEMPOTENCY", None) or {})
    return owners


#: Operations whose effect the durable effect journal settles at most once per
#: request: the replay owner of every write the engine does not replay by key.
EXTERNAL_EFFECT_OPERATIONS: tuple[str, ...] = (
    "identity.self.mfa.totp.begin",
    "identity.self.mfa.totp.confirm",
    "identity.self.mfa.webauthn.register_begin",
    "identity.self.mfa.webauthn.register_finish",
    "identity.self.mfa.recovery.regenerate",
    "identity.self.mfa.credentials.remove",
    "fleet.call",
    "fleet.write_back.commit",
    "fleet.tools.pin",
    "fleet.tools.unpin",
    "messaging.alert",
    "browser.request_call",
    "browser.desktop_action",
    "browser.observe",
    "browser.act",
    "eg.graph.ApplyMutation",
    "access.elevation.request",
    "agents.goals.cancel",
    "agents.goals.run",
    "agents.tasks.cancel",
    "browser.cancel_call",
    "browser.execute_call",
    "browser.issue_lease",
    "browser.reconcile_call",
    "browser.revoke_lease",
    "decide.commit",
    "federation.sources.share",
    "federation.sources.unshare",
    "fleet.catalog.refresh",
    "fleet.tools.load",
    "fleet.tools.unload",
    "ingest.drift.repair_approve",
    "ingest.drift.repair_propose",
    "ingest.embedding.admit",
    "ingest.embedding.reembed",
    "ingest.jobs.cancel",
    "ingest.jobs.prioritize",
    "ingest.repositories.index",
    "ingest.submissions.create",
    "knowledge.curation.propose",
    "knowledge.flag.raise",
    "knowledge.flag.resolve",
    "rlm.run",
    "skills.tier.promote",
    "commands.execute",
    "fleet.skills.toggle",
    "fleet.tools.toggle",
    "ingest.pipeline.run",
    "ingest.sources.connections.create",
    "ingest.sources.discover",
    "ingest.sources.mapping.propose",
    "ingest.sources.runs.cancel",
    "ontology.actions.apply",
    "ontology.document.process",
    "ontology.functions.invoke",
    "ontology.object_sets.delete",
    "ontology.object_sets.save",
    "ontology.object_views.put",
    "ontology.objects.edit",
    "ontology.objects.revert",
    "resources.spawn",
    "roles.instances.create",
    "roles.instances.retire",
    "roles.instances.update",
    "workflows.create",
    "workflows.run",
    "a2a.media.upload",
    "schedules.create",
    "schedules.pause",
    "schedules.resume",
    "schedules.update",
    "approvals.digest.send",
    "orders.paper_submit",
    "orders.change",
    "positions.change",
    "wallet.operate",
)


def _engine_replayed(registry: Any) -> dict[str, str]:
    """Engine methods that replay by the idempotency key the pipeline forwards."""
    from arbora.api.registry import EgMethod

    return {
        op.id: f"medulla:{op.binding.service}"
        for op in registry
        if isinstance(op.binding, EgMethod)
        and op.idempotency == Idempotency.KEY_REQUIRED
    }


def _registered_owners(registry: Any) -> dict[str, str]:
    return {
        op_id: owner
        for op_id, owner in {
            **_engine_replayed(registry),
            **_declared_owners(),
        }.items()
        if registry.get(op_id) is not None
    }


def uncovered_write_operations(registry: Any) -> list[str]:
    """Write, act and admin operations that no replay owner settles.

    An operation is covered by a declared native owner, by the effect journal's
    operation list, or by a plan gate (the one confirmed-replay owner).
    """
    owned = _registered_owners(registry)
    journal = frozenset(EXTERNAL_EFFECT_OPERATIONS)
    return sorted(
        op.id
        for op in registry
        if op.effect != Effect.READ
        and op.id not in owned
        and op.id not in journal
        and op.confirm not in _PLAN_GATED_CONFIRMS
    )


def _with_owned_idempotency(services: Any) -> dict[str, str]:
    """Declared owners plus each registered operation whose authority owns replay.

    ``agents.run`` derives its A2A task id from the idempotency key, so every
    composition that registers it serves it without a separate declaration.
    """
    return {
        **_registered_owners(services.registry),
        **dict(services.native_idempotency),
    }


@dataclass(frozen=True, slots=True)
class InvokeServices:
    registry: Registry
    runtime: OperationRuntime
    plans: PlanStore
    policy_gate: PolicyGate
    audit_preflight: AuditPreflight
    audit_write: AuditWrite
    config: RuntimeConfig
    effects: EffectJournal | None = None
    external_effect_operations: frozenset[str] = frozenset()
    native_idempotency: Mapping[str, str] = field(default_factory=dict)
    fleet_effect: FleetEffect | None = None
    schema_validate: SchemaValidate | None = None
    #: Reads the engine's gate answer when a plan is minted (ARBORA-OPS-R918.2).
    gate_reader: GateReader = no_engine_answer
    #: Operator hooks (ARBORA-OPS-R009.6); empty unless the host binds some.
    hooks: HookChain = field(default_factory=HookChain)
    #: The resource-grant gate for tools that declare a resource
    #: (ARBORA-IDENTITY-R019.3); ``None`` while no gate is composed.
    resource_gate: Any = None
    #: Skill and workflow admission, run before the confirmation gate
    #: (ARBORA-FLEET-R034.3). The default gate admits what declares nothing.
    admission: AdmissionGate = field(default_factory=AdmissionGate)
    #: The engine autonomy verdict reader (ARBORA-OPS-R867); ``None`` until the host binds one.
    autonomy: AskVerdict | None = None

    def __post_init__(self) -> None:
        self._cover_plan_gated_operations()
        self._check_effect_coverage()
        object.__setattr__(self, "native_idempotency", _with_owned_idempotency(self))
        self._check_native_idempotency()
        object.__setattr__(
            self, "native_idempotency", MappingProxyType(dict(self.native_idempotency))
        )
        self._check_authorities()

    def _cover_plan_gated_operations(self) -> None:
        """Put every plan-gated operation under the one confirmed-replay owner.

        A consumed plan cannot prove that its effect completed, so the durable
        effect journal is the single coordinator that settles a retry. Fleet,
        admin and console operations all reach it through this one rule; with
        no journal the operation refuses at the confirmation gate.
        """
        if self.effects is None:
            return
        gated = frozenset(
            op.id for op in self.registry if op.confirm in _PLAN_GATED_CONFIRMS
        )
        if gated and isinstance(self.external_effect_operations, frozenset):
            object.__setattr__(
                self,
                "external_effect_operations",
                self.external_effect_operations | gated,
            )

    def _check_effect_coverage(self) -> None:
        if self.effects is not None and not _has_methods(
            self.effects, ("reserve", "complete")
        ):
            raise ValueError("effect reservation authority unavailable")
        if not isinstance(self.external_effect_operations, frozenset) or any(
            not isinstance(op_id, str) or self.registry.get(op_id) is None
            for op_id in self.external_effect_operations
        ):
            raise ValueError("external effect coverage requires exact operations")
        if self.external_effect_operations and self.effects is None:
            raise ValueError("external effect coverage has no owner authority")

    def _check_native_idempotency(self) -> None:
        if any(
            self.registry.get(op_id) is None
            or not isinstance(owner_contract, str)
            or not owner_contract
            for op_id, owner_contract in self.native_idempotency.items()
        ):
            raise ValueError("invalid native idempotency owner coverage")

    def _check_authorities(self) -> None:
        ports = (
            (self.runtime, _RUNTIME_PORTS),
            (self.plans, _PLAN_PORTS),
            (self.policy_gate, ("check_op",)),
        )
        if not all(_has_methods(owner, names) for owner, names in ports):
            raise ValueError("required invocation authority unavailable")
        if not callable(self.audit_preflight) or not callable(self.audit_write):
            raise ValueError("durable audit authority unavailable")
        needs_schema = any(isinstance(op.params, EgSchemaRef) for op in self.registry)
        if needs_schema and not callable(self.schema_validate):
            raise ValueError("provider schema authority unavailable")
        if self.registry.get("fleet.call") is not None and not callable(
            self.fleet_effect
        ):
            raise ValueError("fleet effect authority unavailable")
