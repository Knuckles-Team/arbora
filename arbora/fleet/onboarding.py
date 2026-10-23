"""Onboard the configured fleet MCP servers into epistemic-graph's catalog.

The fleet catalog joins live ``RegisterServer`` rows with current
``mcp_server`` components. A registration alone admits nothing. Onboarding
therefore does three things for every streamable-HTTP server in the MCP
config:

1. register the server, and renew any lease that lapses soon;
2. capture its served tools, prompts, skills and pack resources as a
   ConnectorPack through the connector SDK;
3. attest that catalog with EG and import the pack under EG's binding;
4. register its access contracts as unapproved virtual mappings.

Arbora observes each child catalog over MCP, as the multiplexer does.
EG issues the binding through ``attest_self_served_catalog``; Arbora must be
the bound importer of each fleet connector. One server's failure never stops
the others. A pass is idempotent, so the boot thread and the operator command
share it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

from arbora.fleet.declarations import RESOURCES_URI, parse_declaration, remember
from arbora.fleet.surfaces import pack_surface_report
from arbora.security.anonymous import session_context

logger = logging.getLogger(__name__)

#: Renew a registration whose lease lapses within this window.
RENEW_MARGIN_MS = 6 * 3600 * 1000
#: How often the boot thread re-runs a pass after the first.
PASS_INTERVAL_S = 1800.0
#: The per-request timeout of one child catalog capture.
CAPTURE_TIMEOUT_S = 60.0
_HTTP_TRANSPORTS = frozenset({"", "http", "streamable-http"})
#: A burst of handshakes for one connector inside this window is one pass.
HANDSHAKE_DEBOUNCE_S = 2.0
#: Retry delays double from the base up to the cap, for at most this many tries.
RETRY_BASE_S = 5.0
RETRY_CAP_S = 300.0
RETRY_MAX_ATTEMPTS = 6
#: The engine code that holds a large pack shrink for operator approval.
MASS_WITHDRAWAL_CODE = "PACK_MASS_WITHDRAWAL"

TriggerReason = Literal[
    "registered", "config_pass", "release_changed", "list_changed", "handshake"
]


@dataclass(frozen=True, slots=True)
class OnboardingTrigger:
    """Why one onboarding pass runs for one connector (ARBORA-FLEET-R041)."""

    connector: str
    reason: TriggerReason
    release_digest: str = ""


@dataclass(frozen=True, slots=True)
class FleetEndpoint:
    """One configured fleet server: registry name and served MCP URL."""

    name: str
    url: str


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


def _endpoint(name: object, spec: object) -> FleetEndpoint | None:
    if not isinstance(name, str) or not isinstance(spec, dict):
        return None
    url = spec.get("url")
    transport = str(spec.get("transport") or spec.get("type") or "")
    if spec.get("disabled") or not isinstance(url, str):
        return None
    if not url.startswith(("http://", "https://")):
        return None
    if transport.replace("_", "-") not in _HTTP_TRANSPORTS:
        return None
    return FleetEndpoint(name=name, url=url)


def fleet_endpoints(
    raw: bytes, *, exclude: Collection[str] = ()
) -> tuple[FleetEndpoint, ...]:
    """Return every enabled streamable-HTTP server in an MCP config payload."""

    from arbora.gateway.config import _parse_mcp_servers

    endpoints = (
        _endpoint(name, spec) for name, spec in _parse_mcp_servers(raw).items()
    )
    return tuple(
        sorted(
            (item for item in endpoints if item and item.name not in exclude),
            key=lambda item: item.name,
        )
    )


def configured_fleet_endpoints(
    *, exclude: Collection[str] = (), path: Path | None = None
) -> tuple[FleetEndpoint, ...]:
    """Read the deployment's MCP config; an absent file has no fleet."""

    from agent_utilities.core.paths import mcp_config_path

    config = path or mcp_config_path()
    if not config.is_file():
        return ()
    return fleet_endpoints(config.read_bytes(), exclude=exclude)


def self_served_connectors() -> tuple[str, ...]:
    """The connectors Arbora serves in process; never fleet children."""

    from arbora.semantic_content import required_content_connectors

    return required_content_connectors()


def _resource_scheme(resource: Any) -> str:
    scheme, separator, _ = str(getattr(resource, "uri", "")).partition("://")
    return scheme if separator else ""


class _PackResourceSession:
    """A session view that lists only ConnectorPack-schemed resources.

    A fleet server may also serve live data resources. Those are not catalog
    content, so the capture never reads them.
    """

    def __init__(self, session: Any) -> None:
        self._session = session

    async def list_resources(self) -> list[Any]:
        from agent_connector_sdk.artifacts.resources import _RESOURCE_SCHEMES

        listed = await self._session.list_resources()
        kept = [
            resource
            for resource in listed
            if _resource_scheme(resource) in _RESOURCE_SCHEMES
            or str(getattr(resource, "uri", "")) == RESOURCES_URI
        ]
        # The catalog names a resource by its pack entry name and reads it by
        # that name. The declaration is served as ``connector-resources``; name
        # it by its URI so the relays read it by URI like every other resource.
        return [
            SimpleNamespace(
                uri=resource.uri,
                name=str(resource.uri),
                mimeType=getattr(resource, "mimeType", None),
                annotations=getattr(resource, "annotations", None),
            )
            if str(getattr(resource, "uri", "")) == RESOURCES_URI
            else resource
            for resource in kept
        ]

    async def read_resource(self, uri: str) -> str:
        return await self._session.read_resource(uri)


#: Seconds between capture requests to one connector. The connector SDK's
#: default per-caller limit is 10 requests/s with a burst of 20, and a pack
#: capture makes one request per skill, prompt and resource, so an unpaced
#: capture of a large pack is throttled mid-way (ARBORA-FLEET-R041.12).
CAPTURE_REQUEST_INTERVAL_S = 0.125


class _PacedSession:
    """Spaces the capture's MCP requests so a connector never throttles them."""

    def __init__(
        self, session: Any, interval_s: float = CAPTURE_REQUEST_INTERVAL_S
    ) -> None:
        import asyncio

        self._session = session
        self._interval_s = interval_s
        self._lock = asyncio.Lock()
        self._last = float("-inf")

    def __getattr__(self, name: str) -> Any:
        import asyncio
        import inspect
        import time

        target = getattr(self._session, name)
        if not inspect.iscoroutinefunction(target):
            return target

        async def paced(*args: Any, **kwargs: Any) -> Any:
            async with self._lock:
                wait = self._last + self._interval_s - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last = time.monotonic()
            return await target(*args, **kwargs)

        return paced


def _capture_kinds() -> tuple[Any, ...]:
    from agent_connector_sdk.artifacts.prompts import PromptArtifactKind
    from agent_connector_sdk.artifacts.resources import ResourceArtifactKind
    from agent_connector_sdk.artifacts.skills import SkillArtifactKind
    from agent_connector_sdk.artifacts.tools import ToolArtifactKind

    class PackResourceKind(ResourceArtifactKind):
        async def list_entries(self, session: Any, server: Any) -> Any:
            # The view implements the McpSession reads by duck typing.
            view = cast(Any, _PackResourceSession(session))
            return await super().list_entries(view, server)

    return (
        ToolArtifactKind(),
        SkillArtifactKind(),
        PromptArtifactKind(),
        PackResourceKind(),
    )


def _remember_declaration(connector: str, pack: Any) -> None:
    """Keep the pack's ``connector://resources`` declaration; absent clears it."""

    declaration = None
    for entry in pack.archive.entries:
        if str(getattr(entry, "uri", "")) == RESOURCES_URI:
            try:
                declaration = parse_declaration(entry.body)
            except (ValueError, TypeError) as exc:
                logger.warning("%s: unreadable %s: %s", connector, RESOURCES_URI, exc)
            break
    remember(connector, declaration)


def _child_auth() -> Any:
    """Arbora's service credential for a child, as the multiplexer sends it."""

    from arbora.mcp_server.backing.client_credentials import child_auth
    from arbora.mcp_server.backing.httpx_boundary import coerce_httpx2_auth

    return coerce_httpx2_auth(child_auth(None))


async def capture_server_pack(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
    """Capture one live fleet server's catalog as a ConnectorPack."""

    from agent_connector_sdk.artifacts.pack import build_content_pack
    from agent_connector_sdk.ports.session import TransportEndpoint
    from agent_connector_sdk.transports.mcp import McpTransport

    transport_endpoint = TransportEndpoint(
        url=endpoint.url, timeout_seconds=CAPTURE_TIMEOUT_S, auth=auth
    )
    async with McpTransport().session(transport_endpoint) as session:
        return await build_content_pack(
            cast(Any, _PacedSession(session)),
            connector=endpoint.name,
            kinds=_capture_kinds(),
        )


@dataclass(frozen=True, slots=True)
class FleetOnboarding:
    """The EG authority one pass runs under."""

    engine: Any
    session: Any
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
    #: The handshake pass record per connector (state, attempts, last error).
    handshake: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def tenant_client(self) -> Any:
        return self.engine.graph_compute.for_graph(str(self.session.graph)).async_client

    @property
    def commons_client(self) -> Any:
        from arbora.deployment.semantic_provisioning import COMMONS_GRAPH

        return self.engine.graph_compute.for_graph(COMMONS_GRAPH).async_client

    async def renew(
        self, endpoints: Mapping[str, str], *, now_ms: int | None = None
    ) -> tuple[str, ...]:
        """Register every endpoint whose lease is absent or lapses soon."""

        from arbora.deployment.semantic_provisioning import (
            COMMONS_GRAPH,
            _bind_session_graph,
            ensure_server_registrations,
        )

        with _bind_session_graph(COMMONS_GRAPH):
            return await ensure_server_registrations(
                self.commons_client,
                endpoints,
                renew_margin_ms=RENEW_MARGIN_MS,
                now_ms=now_ms,
            )

    async def onboard(
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

        from arbora.deployment.semantic_provisioning import import_attested_pack

        if trigger is not None:
            logger.info("%s: onboarding pass (%s)", endpoint.name, trigger.reason)
        capture = self.capture or capture_server_pack
        pack = await capture(endpoint, auth=auth)
        self.surfaces[endpoint.name] = pack_surface_report(pack)
        _remember_declaration(endpoint.name, pack)
        changed = not await self.pack_unchanged(endpoint, pack)
        if changed:
            await import_attested_pack(
                self.engine,
                self.session,
                client=self.tenant_client,
                commons=self.commons_client,
                pack=pack,
                allow_mass_withdrawal=endpoint.name in self.allow_mass_withdrawal,
            )
        else:
            await self.renew({endpoint.name: endpoint.url})
        self.register_contracts(endpoint, pack)
        return changed

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

        from epistemic_graph.connector_pack import pack_digest
        from epistemic_graph.generated.connector_pack import ConnectorPackStatusRequest
        from epistemic_graph.generated.storage import send_connector_pack_status

        from arbora.deployment.semantic_provisioning import _bind_session_graph

        tenant_id = str(session_context(self.session)["tenant"])
        graph = str(self.session.graph)
        with _bind_session_graph(graph):
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
        return head.pack_digest == digest

    def register_contracts(self, endpoint: FleetEndpoint, pack: Any) -> None:
        """Register the pack's access contracts as unapproved mappings."""

        from arbora.fleet.access_contracts import register_access_contracts

        added = register_access_contracts(
            pack, connector=endpoint.name, catalog=self.virtual_catalog
        )
        if added:
            logger.info("%s: %d unapproved access mappings", endpoint.name, added)

    async def self_endpoints(
        self, connectors: Sequence[str], served_url: str
    ) -> dict[str, str]:
        """Self-served registrations to renew, at their configured or live URL."""

        from arbora.deployment.semantic_provisioning import (
            COMMONS_GRAPH,
            _bind_session_graph,
            _registry_page,
        )

        with _bind_session_graph(COMMONS_GRAPH):
            page = await _registry_page(self.commons_client)
        live = {entry.name: entry.url for entry in page.entries}
        urls = {name: served_url or live.get(name, "") for name in connectors}
        return {name: url for name, url in urls.items() if url}


def _failure(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:512]


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
        auth = _child_auth()
    except Exception as exc:  # no service identity: nothing can be captured
        report.failed["child-auth"] = _failure(exc)
        logger.warning("fleet onboarding has no child credential: %s", exc)
        return
    for endpoint in pending:
        try:
            changed = await onboarding.onboard(endpoint, auth=auth)
        except Exception as exc:  # one child must not stop the fleet
            report.failed[endpoint.name] = _failure(exc)
            logger.warning("fleet onboarding of %s failed: %s", endpoint.name, exc)
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
        logger.warning("fleet registration renewal failed: %s", exc)
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
        logger.warning("fleet catalog refresh after onboarding refused: %s", exc)
        return False
    return True


@dataclass(slots=True)
class FleetOnboardingService:
    """The boot-time background pass and its periodic renewal."""

    onboarding: FleetOnboarding
    multiplexer: Any
    served_url: str = ""
    interval_s: float = PASS_INTERVAL_S
    stop: threading.Event = field(default_factory=threading.Event)

    def endpoints(self) -> tuple[FleetEndpoint, ...]:
        exclude = {*self_served_connectors(), *self._skipped()}
        return configured_fleet_endpoints(exclude=exclude)

    def _skipped(self) -> Collection[str]:
        return getattr(self.multiplexer, "_skip_servers", None) or ()

    async def run_pass(self) -> OnboardingReport:
        endpoints = self.endpoints()
        report = await onboard_fleet(
            self.onboarding,
            endpoints,
            served_url=self.served_url,
            admitted=set(self.multiplexer.load_catalog()),
        )
        fleet = [item.name for item in endpoints]
        await refresh_serving_catalog(self.multiplexer, report, fleet)
        self.multiplexer._fleet_onboarding = merge_handshake_records(
            report.as_dict(), self.onboarding.handshake
        )
        logger.info("fleet onboarding pass: %s", report.as_dict())
        return report

    def passes(self) -> Iterator[None]:
        """Yield before each pass: one now, then one per interval until stopped."""

        yield
        while not self.stop.wait(self.interval_s):
            yield

    def run_forever(self) -> None:
        from arbora.api.contracts.session import use_session
        from arbora.security.actor_context import use_actor

        session = self.onboarding.session
        with use_actor(session.actor), use_session(session):
            for _ in self.passes():
                self._run_pass_safely()

    def _run_pass_safely(self) -> None:
        try:
            asyncio.run(self.run_pass())
        except Exception as exc:  # serving must outlive any onboarding fault
            logger.warning("fleet onboarding pass failed: %s", exc)

    def start(self) -> threading.Thread:
        thread = threading.Thread(
            target=self.run_forever, name="fleet-onboarding", daemon=True
        )
        thread.start()
        return thread


def merge_handshake_records(
    snapshot: dict[str, Any], records: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    """Add each connector's handshake record to its R040 surface entry."""

    surfaces = dict(snapshot.get("surfaces", {}))
    for name, record in records.items():
        surfaces[name] = {**surfaces.get(name, {}), "onboarding": dict(record)}
    return {**snapshot, "surfaces": surfaces}


def _digest_rows(rows: Any) -> str:
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, default=str).encode()
    ).hexdigest()


def handshake_catalog_digest(
    tools: Sequence[Any],
    prompts: Sequence[Any] = (),
    resources: Sequence[Any] = (),
    declaration: str = "",
) -> str:
    """A content digest of the served lists and the data-source declaration.

    Covers each tool (name and input schema), prompt (name, description,
    arguments, tags, meta) and resource (uri, mime type) the server lists, plus
    the body of ``connector://resources``. The ``serverInfo`` version is not an
    input: a release that changes nothing served changes nothing here.
    """

    def dump(item: Any) -> Any:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json", by_alias=True, exclude_none=True)
        return str(item)

    return _digest_rows(
        {
            "tools": sorted(
                (
                    str(getattr(t, "name", "")),
                    getattr(t, "inputSchema", None),
                    getattr(t, "meta", None) or getattr(t, "_meta", None),
                )
                for t in tools
            ),
            "prompts": sorted(_digest_rows(dump(p)) for p in prompts),
            "resources": sorted(_digest_rows(dump(r)) for r in resources),
            "declaration": declaration,
        }
    )


async def served_catalog_digest(session: Any, tools: Sequence[Any]) -> str:
    """Digest the lists and resources a live session serves (R041.8).

    A list or read the server does not support counts as empty; that never
    fails the handshake.
    """

    async def optional(call: Any, attr: str) -> list[Any]:
        try:
            return list(getattr(await call(), attr))
        except Exception:
            return []

    prompts = await optional(session.list_prompts, "prompts")
    resources = await optional(session.list_resources, "resources")
    declaration = ""
    if any(str(getattr(r, "uri", "")) == RESOURCES_URI for r in resources):
        try:
            result = await session.read_resource(RESOURCES_URI)
            declaration = "".join(
                str(getattr(c, "text", "") or "") for c in result.contents
            )
        except Exception:
            declaration = ""
    return handshake_catalog_digest(tools, prompts, resources, declaration)


@dataclass(slots=True)
class _Handshake:
    seq: int
    server_version: str
    digest: str
    reason: TriggerReason = "handshake"


@dataclass(slots=True)
class HandshakeOnboarder:
    """Schedules one onboarding pass per completed connector handshake.

    ``notify`` is called by the multiplexer after ``initialize`` and the list
    calls. It only records the handshake and starts (or joins) a worker
    thread, so it never blocks the session or tool serving (R041.7). A burst
    of handshakes for one connector coalesces into one pass. A failed pass is
    logged, recorded per connector, shown in the surface report and retried
    with bounded exponential backoff that resets on success (R041.9). A pass
    held for the mass-withdrawal approval is not retried (R041.10).
    """

    onboarding: FleetOnboarding
    multiplexer: Any
    endpoints: Callable[[], Sequence[FleetEndpoint]]
    debounce_s: float = HANDSHAKE_DEBOUNCE_S
    retry_base_s: float = RETRY_BASE_S
    retry_cap_s: float = RETRY_CAP_S
    max_attempts: int = RETRY_MAX_ATTEMPTS
    stop: threading.Event = field(default_factory=threading.Event)
    #: Waits ``seconds`` and returns True when stopped; replaced in tests.
    wait: Callable[[float], bool] | None = None
    passes_run: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _latest: dict[str, _Handshake] = field(default_factory=dict)
    _active: dict[str, threading.Thread] = field(default_factory=dict)
    _seq: int = 0

    def notify(
        self,
        connector: str,
        *,
        server_version: str,
        digest: str,
        reason: TriggerReason = "handshake",
    ) -> None:
        """Record a completed handshake and schedule one pass; never raises."""

        try:
            with self._lock:
                self._seq += 1
                self._latest[connector] = _Handshake(
                    self._seq, server_version, digest, reason
                )
                if connector in self._active:
                    return
                thread = threading.Thread(
                    target=self._work,
                    args=(connector,),
                    name=f"handshake-onboard-{connector}",
                    daemon=True,
                )
                self._active[connector] = thread
            thread.start()
        except Exception as exc:  # serving must outlive any onboarding fault
            logger.warning(
                "handshake onboarding of %s not scheduled: %s", connector, exc
            )

    def drain(self, timeout: float = 10.0) -> None:
        """Join every running worker; for shutdown and tests."""

        for thread in list(self._active.values()):
            thread.join(timeout)

    def _sleep(self, seconds: float) -> bool:
        return (self.wait or self.stop.wait)(seconds)

    def _work(self, connector: str) -> None:
        try:
            self._loop(connector)
        except Exception as exc:  # pragma: no cover - last-resort guard
            logger.warning("handshake onboarding of %s crashed: %s", connector, exc)
        finally:
            with self._lock:
                self._active.pop(connector, None)

    def _loop(self, connector: str) -> None:
        attempts = 0
        fresh = True
        while True:
            if fresh and self._sleep(self.debounce_s):
                return
            fresh = False
            with self._lock:
                info = self._latest[connector]
            endpoint = next((e for e in self.endpoints() if e.name == connector), None)
            if endpoint is None:
                return  # not a fleet endpoint: nothing to onboard
            status, error = self._attempt(endpoint, info)
            self.passes_run += 1
            if status == "ok":
                with self._lock:
                    if self._latest[connector].seq == info.seq:
                        self._active.pop(connector, None)
                        return
                attempts = 0  # a newer handshake arrived mid-pass: run again
                fresh = True
                continue
            attempts += 1
            if status == "held":
                self._record(connector, info, "awaiting_approval", attempts, error)
                return
            if attempts >= self.max_attempts:
                self._record(connector, info, "failed", attempts, error)
                return
            delay = min(self.retry_cap_s, self.retry_base_s * 2 ** (attempts - 1))
            self._record(connector, info, "failed", attempts, error, delay)
            if self._sleep(delay):
                return

    def _attempt(self, endpoint: FleetEndpoint, info: _Handshake) -> tuple[str, str]:
        """Run one pass; return ``("ok", outcome)``, ``("held", e)`` or ``("error", e)``."""

        from arbora.api.contracts.session import use_session
        from arbora.security.actor_context import use_actor

        session = self.onboarding.session
        trigger = OnboardingTrigger(endpoint.name, info.reason, info.digest)
        try:
            with use_actor(session.actor), use_session(session):
                outcome = asyncio.run(self._pass(endpoint, info, trigger))
        except Exception as exc:
            if MASS_WITHDRAWAL_CODE in str(exc):
                logger.warning(
                    "%s: handshake pass awaits mass-withdrawal approval", endpoint.name
                )
                return "held", _failure(exc)
            logger.warning("%s: handshake onboarding failed: %s", endpoint.name, exc)
            return "error", _failure(exc)
        self._record(endpoint.name, info, outcome, 0, "")
        return "ok", outcome

    async def _pass(
        self, endpoint: FleetEndpoint, info: _Handshake, trigger: OnboardingTrigger
    ) -> str:
        return await self.onboarding.handshake_pass(
            endpoint,
            auth=_child_auth(),
            trigger=trigger,
            server_version=info.server_version,
        )

    def _record(
        self,
        connector: str,
        info: _Handshake,
        state: str,
        attempts: int,
        error: str,
        retry_in_s: float | None = None,
    ) -> None:
        record: dict[str, Any] = {
            "reason": info.reason,
            "state": state,
            "attempts": attempts,
            "server_version": info.server_version,
            "catalog_digest": info.digest,
        }
        if error:
            record["error"] = error
        if retry_in_s is not None:
            record["retry_in_s"] = retry_in_s
        self.onboarding.handshake[connector] = record
        current = self.multiplexer._fleet_onboarding
        self.multiplexer._fleet_onboarding = merge_handshake_records(
            dict(current or {}), {connector: record}
        )


def configured_served_url() -> str:
    """The MCP URL Arbora serves at, from ``ARBORA_SERVED_MCP_URL``."""

    from arbora.core.settings import setting

    return str(setting("ARBORA_SERVED_MCP_URL", "") or "")


def start_fleet_onboarding(
    *, engine: Any, session: Any, multiplexer: Any, served_url: str | None = None
) -> threading.Thread | None:
    """Start the background onboarding service; never raise into boot.

    ``served_url`` defaults to :func:`configured_served_url`. Without it, the
    self-served registrations renew at their live URL.
    """

    if getattr(engine, "graph_compute", None) is None or multiplexer is None:
        logger.warning("fleet onboarding disabled: no engine or multiplexer")
        return None
    service = FleetOnboardingService(
        onboarding=FleetOnboarding(engine=engine, session=session),
        multiplexer=multiplexer,
        served_url=configured_served_url() if served_url is None else served_url,
    )
    onboarder = HandshakeOnboarder(
        onboarding=service.onboarding,
        multiplexer=multiplexer,
        endpoints=service.endpoints,
    )
    setter = getattr(multiplexer, "set_handshake_listener", None)
    if setter is not None:
        setter(onboarder.notify)
    return service.start()


__all__ = [
    "FleetEndpoint",
    "FleetOnboarding",
    "FleetOnboardingService",
    "HandshakeOnboarder",
    "OnboardingReport",
    "OnboardingTrigger",
    "capture_server_pack",
    "configured_fleet_endpoints",
    "fleet_endpoints",
    "onboard_fleet",
    "refresh_serving_catalog",
    "start_fleet_onboarding",
]
