"""Onboard the configured fleet MCP servers into medulla's catalog.

The fleet catalog joins live ``RegisterServer`` rows with current
``mcp_server`` components. A registration alone admits nothing. Onboarding
therefore does three things for every streamable-HTTP server in the MCP
config:

1. register the server, and renew any lease that lapses soon;
2. capture its served tools, prompts, skills and pack resources as a
   ConnectorPack through the connector SDK;
3. attest that catalog with EG and import the pack under EG's binding;
4. register its access contracts as held virtual mappings; an admitted,
   read-only contract may then ask EG's owner-scoped approval operation for a
   receipt before the local projection becomes selectable.

Arbora observes each child catalog over MCP, as the multiplexer does.
EG issues the binding through ``attest_self_served_catalog``; Arbora must be
the bound importer of each fleet connector. One server's failure never stops
the others. A pass is idempotent, so the boot thread and the operator command
share it.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from arbora.access.action_policy import ActionPolicies
from arbora.core.settings import RuntimeConfig
from arbora.fleet.declarations import (
    Declarations,
)
from arbora.fleet.onboarding_capture import (
    FleetEndpoint,
    _child_auth,
    _remember_declaration,
    capture_server_pack,
    self_served_connectors,
)
from arbora.fleet.surfaces import pack_surface_report
from arbora.security.anonymous import session_context

logger = logging.getLogger(__name__)

#: Renew a registration whose lease lapses within this window.
RENEW_MARGIN_MS = 6 * 3600 * 1000

TriggerReason = Literal[
    "registered", "config_pass", "release_changed", "list_changed", "handshake"
]


class ProjectionFailed(RuntimeError):
    """A pack import committed and its projection did not apply (R041.19)."""

    def __init__(self, connector: str, code: str) -> None:
        super().__init__(f"{connector}: imported_projection_failed: {code}")
        self.connector = connector
        self.code = code


@dataclass(frozen=True, slots=True)
class OnboardingTrigger:
    """Why one onboarding pass runs for one connector (ARBORA-FLEET-R041)."""

    connector: str
    reason: TriggerReason
    release_digest: str = ""


@dataclass(frozen=True, slots=True)
class PackSurfaceCounts:
    """What one onboarding pass found a connector serving (ARBORA-FLEET-R041.1).

    Built from the existing ``pack_surface_report`` and the pass's connect
    contract; ``contract_state`` is ``imported``, ``contract_rejected``,
    ``imported_projection_failed`` or ``onboarding_failed``.
    """

    tools: int = 0
    prompts: int = 0
    skills: int = 0
    resources: int = 0
    ontologies: int = 0
    contracts: int = 0
    contract_state: str = "imported"

    @classmethod
    def build(
        cls, report: Mapping[str, Any], contract: Mapping[str, Any] | None = None
    ) -> PackSurfaceCounts:
        served = report.get("surfaces", {})

        def count(name: str) -> int:
            return int(served.get(name, {}).get("count", 0))

        contract = contract or {}
        contracts = (
            (1 if contract.get("manifest_digest") else 0)
            + len(contract.get("ontology_digests") or ())
            + (1 if contract.get("seed_digest") else 0)
        )
        return cls(
            tools=count("tools"),
            prompts=count("prompts"),
            skills=count("skills"),
            resources=count("resources"),
            ontologies=count("ontology"),
            contracts=contracts,
            contract_state=str(contract.get("outcome", "imported")),
        )


@dataclass(slots=True)
class OnboardingReport:
    """What one onboarding pass did, per server name."""

    renewed: list[str] = field(default_factory=list)
    onboarded: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    #: Per-connector surface counts and ``surface_gap`` entries (R040).
    surfaces: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self, *, private: bool = False) -> dict[str, Any]:
        """Render the report; ``private`` keeps only each failure's error type."""

        failed = {
            name: detail.partition(":")[0] if private else detail
            for name, detail in sorted(self.failed.items())
        }
        return {
            "renewed": sorted(self.renewed),
            "onboarded": sorted(self.onboarded),
            "failed": failed,
            "surfaces": {name: self.surfaces[name] for name in sorted(self.surfaces)},
        }


@dataclass(frozen=True, slots=True)
class FleetOnboarding:
    """The EG authority one pass runs under."""

    engine: Any
    session: Any
    #: The fleet's captured ``connector://resources`` declarations.
    declarations: Declarations
    #: The host's policy gates; the pack import is gated through them.
    action_policies: ActionPolicies
    #: Replaces :func:`capture_server_pack`, for example in tests.
    capture: Any = None
    #: The virtual catalog that access contracts register into.
    virtual_catalog: Any = None
    #: Servers whose next import the operator approved to withdraw more than
    #: half of the published surface (a move to one tool per operation).
    allow_mass_withdrawal: frozenset[str] = frozenset()
    #: The last captured surface report per connector, read by the pass.
    surfaces: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: The ``(content digest,)`` last onboarded per connector.
    seen: dict[str, tuple[str, ...]] = field(default_factory=dict)
    #: The last captured connect contract per connector (ARBORA-FLEET-R036).
    contracts: dict[str, Any] = field(default_factory=dict)
    #: The verified graph authority a long pass renews between imports
    #: (ARBORA-FLEET-R041.20): ``near_expiry()`` and ``async renew()``.
    authority: Any = None
    #: The handshake pass record per connector (state, attempts, last error).
    handshake: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Local policy decisions and native EG receipts, keyed by connector.
    source_decisions: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    source_receipts: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    #: Admits versioned definitions before the pack import (ARBORA-FLEET-R002.5);
    #: ``None`` leaves definitions unversioned, as before the engine port is wired.
    definition_admission: Any = None
    #: The host's configuration view (the service credential is read through it).
    config: RuntimeConfig = field(default_factory=RuntimeConfig)

    @property
    def tenant_client(self) -> Any:
        return self.engine.graph_compute.for_graph(str(self.session.graph)).async_client

    @property
    def commons_client(self) -> Any:
        from arbora.semantic_provisioning import COMMONS_GRAPH

        return self.engine.graph_compute.for_graph(COMMONS_GRAPH).async_client

    async def renew(
        self, endpoints: Mapping[str, str], *, now_ms: int | None = None
    ) -> tuple[str, ...]:
        """Register every endpoint whose lease is absent or lapses soon."""

        from arbora.semantic_provisioning import (
            COMMONS_GRAPH,
            bind_session_graph,
            ensure_server_registrations,
        )

        with bind_session_graph(COMMONS_GRAPH):
            return await ensure_server_registrations(
                self.commons_client,
                endpoints,
                renew_margin_ms=RENEW_MARGIN_MS,
                now_ms=now_ms,
            )

    def _record_contract(self, connector: str, pack: Any) -> None:
        """Digest the captured pack and report which resource keys moved.

        The engine decides what to replace and answers ``unchanged`` for equal
        digests; this keeps no second diff authority (ARBORA-FLEET-R036.2).
        """
        from arbora.fleet.connect_contract import contract_delta, contract_from_pack

        current = contract_from_pack(
            connector, pack, observed_at_ms=int(time.time() * 1000)
        )
        changed = contract_delta(self.contracts.get(connector), current)
        self.contracts[connector] = current
        self.surfaces[connector]["contract"] = {
            **current.as_dict(),
            "changed": sorted(changed),
        }

    def _check_contract(self, connector: str, pack: Any) -> None:
        """Refuse an oversized resource before any engine call (R036.4)."""
        from arbora.fleet.connect_contract import ContractRefused, check_resource_sizes

        contract = self.surfaces[connector]["contract"]
        contract["seed"] = "present" if contract.get("seed_digest") else "seed_absent"
        try:
            check_resource_sizes(pack)
        except ContractRefused as exc:
            contract["outcome"] = "contract_rejected"
            contract["reason"] = exc.code
            raise

    async def _admit_definitions(
        self, endpoint: FleetEndpoint, pack: Any
    ) -> tuple[Any, ...]:
        """Admit the pack's versioned definitions before any import (R002.5)."""
        if self.definition_admission is None:
            return ()
        with onboarding_step("DefinitionAdmission"):
            return await self.definition_admission.admit(
                str(self.session.tenant), endpoint.name, pack
            )

    async def _record_definitions(self, receipts: tuple[Any, ...]) -> None:
        """Write the admission receipts after the import (R002.7)."""
        if self.definition_admission is None:
            return
        with onboarding_step("DefinitionReceipt"):
            await self.definition_admission.record(str(self.session.tenant), receipts)

    async def _import_pack(self, endpoint: FleetEndpoint, pack: Any) -> None:
        """Import one pack; an engine contract refusal marks the connector."""
        from arbora.api.contracts import pack_import_authority
        from arbora.fleet.connect_contract import contract_rejection_code
        from arbora.semantic_provisioning import import_attested_pack

        try:
            result = await import_attested_pack(
                self.engine,
                self.session,
                client=self.tenant_client,
                commons=self.commons_client,
                pack=pack,
                action_policies=self.action_policies,
                pack_import_authority=pack_import_authority,
                allow_mass_withdrawal=endpoint.name in self.allow_mass_withdrawal,
            )
        except Exception as exc:
            code = contract_rejection_code(exc)
            if code is not None:
                contract = self.surfaces[endpoint.name]["contract"]
                contract["outcome"] = "contract_rejected"
                contract["reason"] = code
            raise
        self._record_import_receipt(endpoint.name, result)
        self._remember_deployed(
            endpoint.name,
            pack,
            getattr(result, "binding_revision", None),
            str(getattr(result, "pack_digest", "")),
        )

    def _remember_deployed(
        self, connector: str, pack: Any, revision: Any, digest: str
    ) -> None:
        """Keep what is deployed for the conformance check (ARBORA-OPS-R067.10)."""
        from arbora.api.domain.fleet.conformance import (
            DeployedPack,
            live_tool_fingerprints,
        )

        if revision is None or not digest:
            return
        self.declarations.remember_deployed(
            connector,
            DeployedPack(
                revision=int(revision),
                pack_digest=digest,
                tool_fingerprints=live_tool_fingerprints(pack.archive.entries),
            ),
        )

    def _record_import_receipt(self, connector: str, result: Any) -> None:
        """Record the import answer and the engine's seed digest on the contract surface."""
        from arbora.semantic_provisioning import seed_content_digest

        contract = self.surfaces[connector]["contract"]
        contract["import_receipt"] = {
            "result": str(getattr(result, "result", "")),
            "pack_digest": str(getattr(result, "pack_digest", "")),
            "binding_revision": getattr(result, "binding_revision", None),
        }
        engine_seed = seed_content_digest(result)
        if engine_seed:
            contract["seed_content_digest"] = engine_seed

    def _tenant(self) -> str:
        from arbora.semantic_provisioning import session_context

        return str(session_context(self.session)["tenant"])

    async def projection_state(self, connector: str) -> tuple[str, str]:
        """The engine pack status read: the projection state and its stable code."""
        from medulla.generated.connector_pack import ConnectorPackStatusRequest
        from medulla.generated.storage import send_connector_pack_status

        status = await send_connector_pack_status(
            self.tenant_client,
            ConnectorPackStatusRequest(connector=connector, tenant_id=self._tenant()),
            str(self.session.graph),
        )
        projection = status.projection
        return (
            str(getattr(projection, "projection", "none")),
            str(getattr(projection, "code", "") or ""),
        )

    async def reproject(self, connector: str) -> None:
        from arbora.semantic_provisioning import reproject_connector

        await reproject_connector(
            self.tenant_client,
            tenant_id=self._tenant(),
            graph=str(self.session.graph),
            connector=connector,
        )

    async def _check_projection(self, connector: str) -> None:
        """A committed import whose projection failed is reported with its cause.

        ARBORA-FLEET-R041.19: the pass reads the pack status, requests the
        served reproject once, and if the projection still failed marks the
        connector ``imported_projection_failed`` with the engine's stable code
        and the graph name, never a bare refusal. The connector stays unadmitted.
        """
        state, code = await self.projection_state(connector)
        if state != "failed":
            return
        await self.reproject(connector)
        state, code2 = await self.projection_state(connector)
        if state == "applied":
            return
        report = self.surfaces[connector]
        report["onboarding_state"] = "imported_projection_failed"
        report["projection"] = {
            "connector": connector,
            "code": code2 or code or "PROJECTION_FAILED",
            "graph_name": str(self.session.graph),
        }
        raise ProjectionFailed(connector, report["projection"]["code"])

    async def ensure_authority(self) -> None:
        """Renew the graph authority before the next import when it nears expiry."""
        authority = self.authority
        if authority is not None and authority.near_expiry():
            await authority.renew()

    async def onboard_renewing(self, endpoint: FleetEndpoint, *, auth: Any) -> bool:
        """Onboard one connector, renewing a lapsing graph authority first.

        An authority that lapses mid-import is renewed and the pass retried
        once; a connector that still fails is ``onboarding_failed`` with the
        cause ``authority_expired`` (ARBORA-FLEET-R041.20).
        """
        await self.ensure_authority()
        try:
            return await self.onboard(endpoint, auth=auth)
        except Exception:
            cause = self.surfaces.get(endpoint.name, {}).get("cause")
            if cause != "authority_expired" or self.authority is None:
                raise
        await self.authority.renew()
        return await self.onboard(endpoint, auth=auth)

    async def onboard(
        self,
        endpoint: FleetEndpoint,
        *,
        auth: Any,
        trigger: OnboardingTrigger | None = None,
    ) -> bool:
        """One onboarding pass; a failure marks only this connector (R041.4).

        A failed pass records ``onboarding_failed`` (or ``contract_rejected``
        for an engine contract refusal) with a typed cause and re-raises, so
        the pass loop reports it and goes on to the next connector.
        """
        try:
            return await self._onboard_pass(endpoint, auth=auth, trigger=trigger)
        except Exception as exc:
            self._mark_failed(endpoint.name, exc)
            raise

    def _mark_failed(self, connector: str, error: Exception) -> None:
        from arbora.fleet.connect_contract import ContractRefused

        report = self.surfaces.setdefault(connector, {})
        contract = report.get("contract", {})
        if contract.get("outcome") == "contract_rejected":
            state, cause = "contract_rejected", str(contract.get("reason", ""))
        elif isinstance(error, ProjectionFailed):
            state, cause = "imported_projection_failed", error.code
        elif isinstance(error, ContractRefused):
            state, cause = "contract_rejected", error.code
        elif "expired" in str(error).lower():
            state, cause = "onboarding_failed", "authority_expired"
        else:
            state, cause = "onboarding_failed", type(error).__name__
        report["onboarding_state"] = state
        report["cause"] = cause
        report["counts"] = asdict(PackSurfaceCounts.build(report, {"outcome": state}))

    async def _onboard_pass(
        self,
        endpoint: FleetEndpoint,
        *,
        auth: Any,
        trigger: OnboardingTrigger | None = None,
    ) -> bool:
        """Capture one server's catalog; attest and import only when it changed.

        Returns whether EG's catalog actually changed. A server already
        admitted with an unchanged pack is never re-attested: re-attesting
        unchanged content reuses attest's digest-derived idempotency key
        under a freshly randomized request (``request_id``, ``attempt_nonce``,
        ``created_at_ms``), which EG refuses as ``IDEMPOTENCY_CONFLICT``
        (ARBORA-FLEET-R027). Instead it renews the server's own
        registration lease (the same margin-gated call ``_renew_all`` makes
        for every endpoint, so this is a real but usually-idempotent renewal
        opportunity, not a bare skip) and the caller treats it as renewed
        rather than re-onboarded.
        """

        if trigger is not None:
            logger.info("%s: onboarding pass (%s)", endpoint.name, trigger.reason)
        capture = self.capture or capture_server_pack
        with onboarding_step("capture"):
            pack = await capture(endpoint, auth=auth)
        self.surfaces[endpoint.name] = pack_surface_report(pack)
        self._record_contract(endpoint.name, pack)
        self._check_contract(endpoint.name, pack)
        if trigger is not None:
            self.surfaces[endpoint.name]["trigger"] = {
                "reason": trigger.reason,
                "release_digest": trigger.release_digest,
            }
        _remember_declaration(self.declarations, endpoint.name, pack)
        receipts = await self._admit_definitions(endpoint, pack)
        with onboarding_step("ConnectorPackStatus"):
            changed = not await self.pack_unchanged(endpoint, pack)
        if changed:
            with onboarding_step("ConnectorPackImport"):
                await self._import_pack(endpoint, pack)
            await self._record_definitions(receipts)
            await self._check_projection(endpoint.name)
        else:
            with onboarding_step("renew"):
                await self.renew({endpoint.name: endpoint.url})
        # Keep the hook-compatible ``None`` return accepted for lightweight
        # callers that override the pre-approval registration step.
        with onboarding_step("contracts"):
            decisions = self.register_contracts(endpoint, pack) or ()
            await self.approve_contracts(endpoint, pack, decisions)
        await self.write_catalog(endpoint, pack)
        report = self.surfaces[endpoint.name]
        report["onboarding_state"] = "onboarded"
        report["counts"] = asdict(
            PackSurfaceCounts.build(report, report.get("contract", {}))
        )
        return changed

    async def write_catalog(self, endpoint: FleetEndpoint, pack: Any) -> None:
        """Write the pack's agent-library catalog nodes (ARBORA-FLEET-R041.17).

        Runs on every pass, imported or renewed: ``AddTriples`` of the same
        set is idempotent, and a store that predates the catalog gains it on
        the next pass. A failed write never fails onboarding; it is reported
        as a ``catalog`` surface gap.
        """

        from arbora.fleet.catalog_graph import write_catalog
        from arbora.semantic_provisioning import (
            bind_session_graph,
            catalog_content_digest,
        )

        report = self.surfaces.setdefault(endpoint.name, {})
        try:
            graph = str(self.session.graph)
            with bind_session_graph(graph):
                count = await write_catalog(
                    self.tenant_client,
                    graph,
                    endpoint.name,
                    pack,
                    tenant=self._tenant(),
                    importer=str(getattr(self.session.actor, "actor_id", "")),
                    version=catalog_content_digest(pack),
                )
        except Exception as exc:  # reported, never fatal
            logger.warning(
                "%s: catalog write failed: %s", endpoint.name, exc, exc_info=True
            )
            if isinstance(report, dict):
                report["catalog_error"] = _failure(exc)
            return
        if isinstance(report, dict):
            report["catalog_triples"] = count

    async def handshake_pass(
        self,
        endpoint: FleetEndpoint,
        *,
        auth: Any,
        trigger: OnboardingTrigger,
        server_version: str,
    ) -> str:
        """Run one handshake-triggered pass; return ``onboarded`` or ``renewed``.

        A reconnect that shows the same content digest of the served lists and
        resources as the last onboarded pack captures nothing and only renews
        the lease (ARBORA-FLEET-R041.8). The ``serverInfo`` version is
        recorded, never compared. Any other handshake goes through the one
        :meth:`onboard`, which still compares with the pack EG holds.
        """

        key = (trigger.release_digest,)
        # An import needs a live registration: renew first (margin-gated).
        await self.renew({endpoint.name: endpoint.url})
        if self.seen.get(endpoint.name) == key:
            return "renewed"
        changed = await self.onboard(endpoint, auth=auth, trigger=trigger)
        self.seen[endpoint.name] = key
        return "onboarded" if changed else "renewed"

    async def pack_unchanged(self, endpoint: FleetEndpoint, pack: Any) -> bool:
        """True when EG already holds ``pack`` unchanged under its current head.

        Reads EG's durable ``ConnectorPack.status`` for the connector -- a
        status read, never a mutation -- and recomputes the pack digest with
        the catalog binding the head itself carries. A mismatch (new content)
        or an absent head (never onboarded) both mean "not unchanged", so the
        caller proceeds to a real attest and import.
        """

        from medulla.connector_pack import pack_digest
        from medulla.generated.connector_pack import ConnectorPackStatusRequest
        from medulla.generated.storage import send_connector_pack_status

        from arbora.semantic_provisioning import bind_session_graph

        tenant_id = str(session_context(self.session)["tenant"])
        graph = str(self.session.graph)
        with bind_session_graph(graph):
            status = await send_connector_pack_status(
                self.tenant_client,
                ConnectorPackStatusRequest(
                    connector=endpoint.name, tenant_id=tenant_id
                ),
                graph,
            )
        head = status.head
        if head is None:
            return False
        digest = pack_digest(
            endpoint.name, head.catalog, pack.archive.server, pack.archive.entries
        )
        return self._head_matches(endpoint.name, pack, head, digest)

    def _head_matches(self, connector: str, pack: Any, head: Any, digest: str) -> bool:
        """Compare the head's digest; an unchanged head is what is deployed."""
        unchanged = head.pack_digest == digest
        if unchanged:
            self._remember_deployed(
                connector, pack, head.binding_revision, str(head.pack_digest)
            )
        return unchanged

    def register_contracts(
        self, endpoint: FleetEndpoint, pack: Any
    ) -> tuple[Any, ...] | None:
        """Register candidates and evaluate the local admission policy.

        The returned decisions are policy intent only.  ``approve_contracts``
        must obtain and validate an EG receipt before a mapping is selectable.
        """

        from arbora.fleet.access_contracts import register_access_contracts

        if self.virtual_catalog is None:
            # Direct CLI/unit callers may construct FleetOnboarding without
            # the process composition root.  Give that isolated pass its own
            # metadata projection; only ``start_fleet_onboarding`` installs
            # the process singleton used by the served server.
            from arbora.fleet.virtual_catalog import VirtualCatalog

            catalog = VirtualCatalog()
            object.__setattr__(self, "virtual_catalog", catalog)
        decisions: list[Any] = []
        added = register_access_contracts(
            pack,
            connector=endpoint.name,
            catalog=self.virtual_catalog,
            admitted=True,
            decisions=decisions,
        )
        from arbora.fleet.access_contracts import pack_contract_resources

        digests = {
            str(getattr(contract, "contract_iri", "")): digest
            for contract, _resource_uri, digest in pack_contract_resources(pack)
        }
        result = tuple(
            replace(
                decision,
                contract_digest=digests.get(str(getattr(decision, "contract_iri", ""))),
            )
            if hasattr(decision, "contract_digest")
            else decision
            for decision in decisions
        )
        self.source_decisions[endpoint.name] = result
        if added:
            logger.info("%s: %d unapproved access mappings", endpoint.name, added)
        return result

    async def approve_contracts(
        self, endpoint: FleetEndpoint, pack: Any, decisions: Sequence[Any]
    ) -> None:
        """Project native EG source-approval receipts onto this catalog.

        The installed EG contract may predate ``ConnectorPack.approve_source``.
        In that case the mapping remains held and the typed unavailable reason
        is recorded; no local flag or synthetic caller can make it readable.
        """

        approved = {
            str(getattr(decision, "contract_iri", "")): decision
            for decision in decisions
            if getattr(decision, "outcome", None) == "approved"
        }
        if not approved:
            self._record_source_approvals(endpoint.name, decisions, ())
            return
        try:
            from importlib import import_module

            connector_pack = import_module("medulla.generated.connector_pack")
            models = import_module("medulla.generated.models")
            storage = import_module("medulla.generated.storage")
            ConnectorPackOpApproveSource = connector_pack.__dict__[
                "ConnectorPackOpApproveSource"
            ]
            ConnectorPackSourceApprovalRequest = connector_pack.__dict__[
                "ConnectorPackSourceApprovalRequest"
            ]
            ConnectorPackStatusRequest = connector_pack.__dict__[
                "ConnectorPackStatusRequest"
            ]
            PackHeadRef = connector_pack.__dict__["PackHeadRef"]
            ConnectorPackSourceApprovalResult = models.__dict__[
                "ConnectorPackSourceApprovalResult"
            ]
            send_connector_pack_status = storage.__dict__["send_connector_pack_status"]
        except (ImportError, KeyError):
            reason = "medulla does not serve ConnectorPack.approve_source yet"
            self._record_source_approvals(
                endpoint.name,
                decisions,
                (),
                pending=reason,
            )
            return

        from arbora.api.errors import ArboraRefusal
        from arbora.semantic_provisioning import bind_session_graph, shell_context

        tenant_id = str(session_context(self.session)["tenant"])
        graph = str(self.session.graph)
        try:
            with bind_session_graph(graph):
                status = await send_connector_pack_status(
                    self.tenant_client,
                    ConnectorPackStatusRequest(
                        connector=endpoint.name, tenant_id=tenant_id
                    ),
                    graph,
                )
        except Exception as exc:
            logging.getLogger(__name__).debug("retry on the next pass", exc_info=True)
            self._record_source_approvals(
                endpoint.name, decisions, (), pending=_failure(exc)
            )
            return
        head = getattr(status, "head", None)
        if head is None:
            self._record_source_approvals(
                endpoint.name,
                decisions,
                (),
                pending="connector pack has no current head for source approval",
            )
            return
        expected_head = PackHeadRef(
            binding_revision=int(head.binding_revision),
            pack_digest=str(head.pack_digest),
        )
        receipts: list[Any] = []
        resolved = list(decisions)
        from arbora.fleet.access_contracts import (
            bind_approved_mapping,
            pack_contract_resources,
        )

        for contract, resource_uri, contract_digest in pack_contract_resources(pack):
            decision = approved.get(str(getattr(contract, "contract_iri", "")))
            if decision is None:
                continue
            key = (
                f"arbora:source-approval:{tenant_id}:{endpoint.name}:"
                f"{getattr(contract, 'contract_iri', '')}:"
                f"{head.binding_revision}:{contract_digest}"
            )
            try:
                context = shell_context(tenant_id, key)
                request = ConnectorPackSourceApprovalRequest(
                    connector=endpoint.name,
                    context=context,
                    contract_digest=contract_digest,
                    contract_iri=str(getattr(contract, "contract_iri", "")),
                    expected_head=expected_head,
                    resource_uri=resource_uri,
                )
                operation = ConnectorPackOpApproveSource(
                    op="approve_source", request=request
                )
                with bind_session_graph(graph):
                    payload = await self.tenant_client._send(
                        "ConnectorPack",
                        {"op": operation.model_dump(mode="json")},
                        graph,
                        idempotency_key=key,
                    )
                receipt = ConnectorPackSourceApprovalResult.model_validate(payload)
                receipt_head = getattr(receipt, "head", None)
                if (
                    str(getattr(receipt, "connector", "")) != endpoint.name
                    or str(getattr(receipt, "tenant_id", "")) != tenant_id
                    or str(getattr(receipt, "contract_digest", "")) != contract_digest
                    or receipt_head is None
                    or int(getattr(receipt_head, "binding_revision", -1))
                    != int(expected_head.binding_revision)
                    or str(getattr(receipt_head, "pack_digest", ""))
                    != str(expected_head.pack_digest)
                ):
                    logger.warning(
                        "%s: native source approval receipt failed identity/CAS validation",
                        endpoint.name,
                    )
                    continue
                bound = bind_approved_mapping(
                    self.virtual_catalog,
                    contract,
                    receipt,
                    connector=endpoint.name,
                    resource_uri=resource_uri,
                    policy_id=decision.policy_id,
                    scope=(tenant_id, graph),
                )
                if bound.outcome == "approved":
                    receipts.append(receipt)
                    for index, decision_item in enumerate(resolved):
                        if decision_item is decision and hasattr(
                            decision_item, "receipt"
                        ):
                            resolved[index] = replace(
                                decision_item,
                                contract_digest=contract_digest,
                                receipt=receipt,
                            )
                            break
                else:
                    logger.warning(
                        "%s: native source approval did not match catalog: %s",
                        endpoint.name,
                        bound.reason,
                    )
            except ArboraRefusal as exc:
                self.source_decisions[endpoint.name] = tuple(resolved)
                self._record_source_approvals(
                    endpoint.name, resolved, tuple(receipts), pending=str(exc)
                )
                return
            except Exception as exc:  # retry the source next pass
                logger.warning(
                    "%s: source approval for %s is pending: %s",
                    endpoint.name,
                    getattr(contract, "contract_iri", ""),
                    type(exc).__name__,
                    exc_info=True,
                )
        self.source_decisions[endpoint.name] = tuple(resolved)
        self._record_source_approvals(endpoint.name, resolved, tuple(receipts))

    def _record_source_approvals(
        self,
        connector: str,
        decisions: Sequence[Any],
        receipts: Sequence[Any],
        *,
        pending: str | None = None,
    ) -> None:
        self.source_receipts[connector] = tuple(receipts)
        record = getattr(self.virtual_catalog, "record_source_decisions", None)
        if callable(record):
            record(connector, decisions)
        report = self.surfaces.setdefault(connector, {})
        if not isinstance(report, dict):
            return
        report["source_approvals"] = {
            "approved": sum(
                1 for item in decisions if getattr(item, "outcome", None) == "approved"
            ),
            "held": sum(
                1 for item in decisions if getattr(item, "outcome", None) == "held"
            ),
            "receipts": len(receipts),
        }
        if pending:
            report["sources_pending"] = pending[:256]

    async def self_endpoints(
        self, connectors: Sequence[str], served_url: str
    ) -> dict[str, str]:
        """Self-served registrations to renew, at their configured or live URL."""

        from arbora.semantic_provisioning import (
            COMMONS_GRAPH,
            bind_session_graph,
            registry_page,
        )

        with bind_session_graph(COMMONS_GRAPH):
            page = await registry_page(self.commons_client)
        live = {entry.name: entry.url for entry in page.entries}
        urls = {name: served_url or live.get(name, "") for name in connectors}
        return {name: url for name, url in urls.items() if url}


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:512]


#: The engine's own detail when it could not classify a refusal.
_UNCLASSIFIED = "unclassified engine refusal"


class OnboardingStepRefused(Exception):
    """One onboarding step's refusal, with the engine refusal code (R064)."""

    def __init__(self, step: str, code: str, detail: str) -> None:
        self.step = step
        self.code = code
        self.detail = detail
        hint = " (engine-side classification gap)" if _UNCLASSIFIED in detail else ""
        super().__init__(f"{step} refused {code}: {detail}{hint}")


@contextlib.contextmanager
def onboarding_step(step: str) -> Iterator[None]:
    """Label an engine refusal with the onboarding step that met it.

    The engine refusal code (``EngineResponseError.code``) is kept as is,
    never folded into a generic message (ARBORA-FLEET-R064).
    """

    try:
        yield
    except OnboardingStepRefused:
        raise
    except Exception as exc:
        code = getattr(exc, "code", None)
        if not isinstance(code, str) or not code:
            raise
        detail = str(getattr(exc, "detail", None) or getattr(exc, "message", "") or "")
        if not detail:
            detail = str(exc).removeprefix(f"{code}: ")
        raise OnboardingStepRefused(step, code, detail[:400]) from exc


async def _renew_all(
    onboarding: FleetOnboarding,
    endpoints: Sequence[FleetEndpoint],
    *,
    served_url: str,
    report: OnboardingReport,
) -> None:
    targets = await onboarding.self_endpoints(self_served_connectors(), served_url)
    targets.update({item.name: item.url for item in endpoints})
    report.renewed.extend(await onboarding.renew(targets))


async def _onboard_all(
    onboarding: FleetOnboarding,
    pending: Sequence[FleetEndpoint],
    report: OnboardingReport,
) -> None:
    if not pending:
        return
    try:
        auth = _child_auth(onboarding.config)
    except Exception as exc:  # no service identity: nothing can be captured
        report.failed["child-auth"] = _failure(exc)
        logger.warning(
            "fleet onboarding has no child credential: %s", exc, exc_info=True
        )
        return
    for endpoint in pending:
        try:
            changed = await onboarding.onboard_renewing(endpoint, auth=auth)
        except Exception as exc:  # one child must not stop the fleet
            report.failed[endpoint.name] = _failure(exc)
            logger.warning(
                "fleet onboarding of %s failed: %s", endpoint.name, exc, exc_info=True
            )
        else:
            (report.onboarded if changed else report.renewed).append(endpoint.name)
        surfaces = onboarding.surfaces.get(endpoint.name)
        if surfaces is not None:
            report.surfaces[endpoint.name] = surfaces


async def onboard_fleet(
    onboarding: FleetOnboarding,
    endpoints: Sequence[FleetEndpoint],
    *,
    served_url: str = "",
    admitted: Collection[str] = (),
) -> OnboardingReport:
    """Run one idempotent pass: renew every lease, onboard every new server.

    ``admitted`` names servers the serving catalog already holds; a pass
    skips their capture and import but still renews their leases.
    """

    report = OnboardingReport()
    try:
        await _renew_all(onboarding, endpoints, served_url=served_url, report=report)
    except Exception as exc:  # the registry may be briefly unavailable
        report.failed["registry"] = _failure(exc)
        logger.warning("fleet registration renewal failed: %s", exc, exc_info=True)
        return report
    pending = [item for item in endpoints if item.name not in admitted]
    await _onboard_all(onboarding, pending, report)
    return report


async def refresh_serving_catalog(
    multiplexer: Any, report: OnboardingReport, fleet: Collection[str]
) -> bool:
    """Re-read the EG catalog when a pass changed what it can admit.

    The multiplexer refuses a refresh once a child runs. Then the new
    servers wait for the next restart, and the refusal is logged. The
    refresh runs on the onboarding thread; it swaps the catalog only while
    no child session exists.
    """

    current = set(multiplexer.load_catalog())
    changed = (set(report.onboarded) | set(report.renewed)) & set(fleet)
    if not changed - current:
        return False
    try:
        await multiplexer.refresh_engine_catalog()
    except Exception as exc:  # serving continues on the prior catalog
        logger.warning(
            "fleet catalog refresh after onboarding refused: %s", exc, exc_info=True
        )
        return False
    return True


__all__ = [
    "PackSurfaceCounts",
    "ProjectionFailed",
    "FleetOnboarding",
    "OnboardingReport",
    "OnboardingTrigger",
    "onboard_fleet",
    "refresh_serving_catalog",
]
