"""The ports an operation handler calls instead of importing a subsystem (ARBORA-HOST-R038.1).

A handler lives in the operation layer and a subsystem lives above it, so the handler declares
what it needs as a ``typing.Protocol`` here and the subsystem implements it. The host composes
one :class:`OperationPorts` record and binds it in ``context.services`` under
:data:`PORTS_BINDING`; a handler reads only the port it uses with :func:`port_of`. A port that
the host did not compose is refused ``UNAVAILABLE``; no handler falls back to a default
construction of a subsystem.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

from arbora.api.invoke.pipeline import OperationRefused

__all__ = [
    "PORTS_BINDING",
    "AppAdmission",
    "CapacityLeases",
    "FleetAcceptance",
    "NativeRunIdentity",
    "OperationPorts",
    "PlanPublication",
    "RunExecutors",
    "RunSettings",
    "WorkItems",
    "configured_connectors",
    "port_if_composed",
    "port_of",
]

#: The ``context.services`` key the host binds the record under.
PORTS_BINDING = "operation_ports"


class AppAdmission(Protocol):
    """Application-source admission: the dry-run plan, the durable run and retirement."""

    def plan(self, tenant: str, params: Mapping[str, Any]) -> dict[str, Any]: ...

    async def admit(
        self, context: Any, params: Mapping[str, Any], *, fence: str
    ) -> dict[str, Any]: ...

    async def retire(
        self, context: Any, run_id: str, *, approve: bool
    ) -> dict[str, Any]: ...


class FleetAcceptance(Protocol):
    """The fleet side of connector acceptance: what is configured and what a pack looks like."""

    def configured(self) -> tuple[str, ...]: ...

    async def digest_matches(self, context: Any, connector: str) -> bool | None: ...


class NativeRunIdentity(Protocol):
    """The A2A identity a natively admitted run carries: owner, digests and the schema."""

    metadata_schema: str

    def owner_ref(self, session: Any) -> str: ...

    def digest(self, value: Any) -> str: ...

    def opaque_work_item_id(self, item_id: str) -> str: ...

    def owned(self, item: Any, owner_ref: str) -> bool: ...


class WorkItems(Protocol):
    """The durable work item store a plan or research run submits to, claims and commits."""

    TERMINAL_WORK_ITEM_STATUSES: Collection[str]

    def submit_work_item_atomic(
        self, engine: Any, /, **submission: Any
    ) -> tuple[str, bool]: ...

    def get_work_item(self, engine: Any, item_id: str, /) -> Any: ...

    def claim_specific(
        self, engine: Any, item_id: str, /, **claim: Any
    ) -> Mapping[str, Any] | None: ...

    def commit_result(
        self, engine: Any, item_id: str, claim: Any, /, **outcome: Any
    ) -> str: ...

    def native_owner_graph(self, engine: Any, /) -> str | None: ...


class RunSettings(Protocol):
    """The agent run bindings a plan run reads: the model registry and the native run scope."""

    def run_config(self, config: Any, /) -> Mapping[str, Any]: ...

    def resolve_model(self, model_id: str, /, *, config: Any) -> Any: ...

    def native_run_scope(self, **scope: Any) -> AbstractContextManager[Any]: ...


class RunExecutors(Protocol):
    """The factory of the agent execution port a control plane runs agents through."""

    def __call__(self, *, origins: Any, ports: Any, config: Any) -> Any: ...


class PlanPublication(Protocol):
    """The publication scope that binds an admitted claim around a plan run."""

    def bind_admitted_claim(
        self, engine: Any, /, **claim: Any
    ) -> AbstractContextManager[Any]: ...


class CapacityLeases(Protocol):
    """The engine's build-slot lease family (ARBORA-CAPACITY-R754.1).

    The holder and tenant are arguments the handler derives from the verified caller; the
    port builds the engine request from them and returns the engine's lease record.
    """

    async def request(
        self, context: Any, *, owner: str, tenant: str, params: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def mutate(
        self,
        context: Any,
        method: str,
        *,
        owner: str,
        tenant: str,
        params: Mapping[str, Any],
    ) -> dict[str, Any]: ...

    async def revoke(
        self, context: Any, *, tenant: str, params: Mapping[str, Any]
    ) -> dict[str, Any]: ...

    async def status(
        self, context: Any, *, tenant: str, lease_id: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class OperationPorts:
    """One field per port; the host composes the fields it serves and leaves the rest unbound."""

    apps: AppAdmission | None = None
    fleet_acceptance: FleetAcceptance | None = None
    run_identity: NativeRunIdentity | None = None
    work_items: WorkItems | None = None
    run_settings: RunSettings | None = None
    plan_publication: PlanPublication | None = None
    run_executors: RunExecutors | None = None
    capacity_leases: CapacityLeases | None = None

    @classmethod
    def unbound(cls) -> OperationPorts:
        """A record with no port composed: every handler that needs one refuses."""
        return cls()


def port_if_composed(context: Any, name: str) -> Any:
    """The named port of the composed record, or ``None`` when the host did not compose it."""
    ports = (context.services or {}).get(PORTS_BINDING)
    return getattr(ports, name, None)


def port_of(context: Any, name: str) -> Any:
    """The named port of the composed record, or refuse ``UNAVAILABLE``."""
    port = port_if_composed(context, name)
    if port is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": f"the {name} port is not composed"}
        )
    return port


def configured_connectors(context: Any) -> tuple[str, ...]:
    """The connectors the composed fleet acceptance port names; none when it is not composed."""
    port = port_if_composed(context, "fleet_acceptance")
    if port is None:
        return ()
    return port.configured()
