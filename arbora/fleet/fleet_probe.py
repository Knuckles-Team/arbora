"""Probing the fleet: harvesting each child's tools, prompts and skills into the probe cache (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import asyncio
import collections.abc as _collections_abc
import contextlib
import logging
import re
import time
import typing as _typing

from mcp.client.session import ClientSession

if _typing.TYPE_CHECKING:
    pass
import arbora.core.priority as _resource_priority
import arbora.fleet.child_resilience as _child_resilience
from arbora.fleet.child_payload_bounds import (
    _bounded_descriptor_catalog,
    _bounded_prompt_catalog,
    _bounded_skill_catalog,
    _bounded_tool_catalog,
    _list_all,
    _list_all_tools,
)
from arbora.fleet.child_runtime_policy import (
    _close_runtime_child_policy,
    _prepare_runtime_child_policy,
)
from arbora.fleet.child_transport import (
    _CURRENT_DISCOVERY_BINDING,
)
from arbora.fleet.local_skill_catalog import (
    build_local_skill_catalog,
    missing_core_pack_names,
)
from arbora.fleet.oauth_brokers import oauth_gated
from arbora.security.log_redaction import redact_for_log

logger = logging.getLogger("mcp_multiplexer")

# Fleet-wide concurrent-probe ceiling (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog), shared
# across overlapping ``probe_catalog`` calls via ``MCPMultiplexer._probe_semaphore``
# (one instance per multiplexer, NOT re-created per call — a per-call semaphore
# would give every new call its own fresh 16 slots regardless of how many probes
# an EARLIER call already left running, defeating the point of a shared cap).
# Measured (see docs/architecture/fleet-catalog-discovery-budget.md): a cold
# stdio child's own connect+handshake already costs ~2.7-2.9s on this hardware
# before any real work, so a 61-server fleet queued 16-wide needed 4 full waves
# to even ATTEMPT every server once — comfortably exceeding any interactive
# budget on its own, before counting genuinely slow/unreachable servers.
#
# LOWERED 32 -> 8 (BUG-PE-055). That reasoning optimised for "attempt every
# server soon" and ignored that each probe carries its OWN wall-clock timeout:
# raising concurrency past what one event loop can actually service makes every
# in-flight probe MISS that timeout, so the sweep attempts more servers and
# finishes fewer. Measured live 2026-08-25, same 66-server homelab fleet, same
# process, only this number varied (successful servers / tools written):
#
#     32 ->  4/66,   361 tools      8 -> 65/66, 7975 tools
#      6 -> 58/66,  6291 tools      4 -> 59/66, 7422 tools
#      3 -> 60/66,  9008 tools
#
# Servers that individually probe in 3.5-7.8s were uniformly failing their own
# 10-15s deadline at 32-wide — one loop decoding ~10k tool schemas cannot keep
# 32 probes inside their deadlines. 8 completes the fleet in ~55s, inside the
# connectors lane's 108s fleet-probe budget, with room to spare.
_PROBE_CONCURRENCY = 8


_SERVER_DISCOVERY_STOPWORDS = frozenset({"api", "mcp", "manager", "server", "service"})


def _probe_timeout_seconds(cfg: dict, timeout: float | None) -> float:
    """This probe's own deadline in seconds; ``0.0`` when the declaration is
    unusable (the caller rejects anything outside its safety boundary)."""

    try:
        return float(
            timeout
            if timeout is not None
            else cfg.get("probe_timeout", cfg.get("timeout", 10.0))
        )
    except (TypeError, ValueError):
        return 0.0


async def _run_bounded_probe(
    probe: _typing.Any, probe_to: float
) -> tuple[dict, _typing.Any]:
    """Run one probe coroutine under its deadline into ``(info, binding)``.

    Every failure mode becomes an honest ``error`` entry rather than an
    exception: a probe of an unreachable server must not fail the sweep.
    """

    try:
        (
            tools,
            resources,
            resource_templates,
            native_prompts,
            skills,
            prompts,
            family_errors,
            binding,
        ) = await asyncio.wait_for(probe(), timeout=probe_to)
    except TimeoutError:
        return (
            {
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "native_prompts": [],
                "catalog_family_errors": {},
                "skills": [],
                "prompts": [],
                "error": f"timeout after {probe_to:g}s",
            },
            None,
        )
    except Exception as e:
        logger.warning("_run_bounded_probe failed", exc_info=True)
        return (
            {
                "tools": [],
                "resources": [],
                "resource_templates": [],
                "native_prompts": [],
                "catalog_family_errors": {},
                "skills": [],
                "prompts": [],
                "error": _format_probe_error(e),
            },
            None,
        )
    return (
        {
            "tools": tools,
            "resources": resources,
            "resource_templates": resource_templates,
            "native_prompts": native_prompts,
            "catalog_family_errors": family_errors,
            "skills": skills,
            "prompts": prompts,
            "error": None,
        },
        binding,
    )


def _format_probe_error(exc: BaseException) -> str:
    """Render leaf exception types **and their messages** from a probe failure.

    Reporting only ``type(exc).__name__`` (the prior behavior) collapses every
    distinct failure — a DNS error, a TLS failure, and a JWT issuer mismatch —
    into the same bare "RuntimeError", making `find_tools`/`load_tools`/
    `probe_catalog` undiagnosable from the caller's side. The leaf message is
    the actual signal (e.g. an auth server's "issuer mismatch (got X, expected
    Y)"), so it is included, truncated to a bounded length per leaf so one
    verbose exception can't blow out the aggregate catalog response."""
    leaves: list[str] = []

    def _walk(e: BaseException) -> None:
        subs = getattr(e, "exceptions", None)
        if isinstance(e, BaseExceptionGroup) or (
            subs and isinstance(subs, list | tuple)
        ):
            for sub in subs or []:
                _walk(sub)
        else:
            name = type(e).__name__
            msg = str(e).strip()
            leaves.append(f"{name}: {msg[:300]}" if msg else name)

    _walk(exc)
    # de-dup while preserving order
    seen = list(dict.fromkeys(leaves))
    return "; ".join(seen) if seen else type(exc).__name__


def _tenant_local_discovery_binding() -> _typing.Any | None:
    """Mint the non-OAuth discovery visibility contract from verified state.

    Local/stdio children have no provider grant to resolve.  Their discovery
    is still not caller-authorized: the process-owned multiplexer may expose a
    tenant-local snapshot only while a verified graph session is ambient.  Do
    not derive an OAuth-like digest from roles/scopes or accept catalog fields.
    """
    try:
        from arbora.fleet.discovery_authority import (
            TenantLocalDiscoveryBinding,
        )
        from arbora.security.session import current_session

        session = current_session()
        if session is None or not getattr(session.actor, "authenticated", False):
            return None
        tenant = str(session.tenant or "").strip()
        if not tenant:
            return None
        return TenantLocalDiscoveryBinding(tenant_id=tenant)
    except (ImportError, PermissionError, TypeError, ValueError):
        return None


class FleetProbe:
    """Probes each fleet server and caches what it harvested.

    Built from the shared state and the calls it needs, with no reference to the
    multiplexer that owns that state."""

    def __init__(
        self,
        *,
        fleet: _typing.Any,
        children: dict[str, _typing.Any],
        sessions: dict[str, ClientSession],
        transport: _typing.Any,
        probe_cache: dict[str, dict],
        probe_inflight: dict[str, asyncio.Task],
        probe_tasks: set[asyncio.Task],
        child_policy_admitted_tools: dict[str, frozenset[str]],
        discovery_binding_sidechannel: dict[
            int, tuple[str, dict[str, _typing.Any], _typing.Any]
        ],
        local_discovery_cache_authority: dict[
            int, tuple[str, dict[str, _typing.Any], _typing.Any]
        ],
        fleet_catalog: _typing.Callable[[], _typing.Any],
        fleet_catalog_loaded_at: _typing.Callable[[], float | None],
        tool_rows: _typing.Callable[[], _typing.Any],
        mounts: _typing.Callable[[], _typing.Any],
        load_catalog: _typing.Callable[..., _typing.Any],
    ) -> None:
        self._fleet = fleet
        self.children = children
        self.sessions = sessions
        self.transport = transport
        self._probe_cache = probe_cache
        self._probe_inflight = probe_inflight
        self._probe_tasks = probe_tasks
        self._child_policy_admitted_tools = child_policy_admitted_tools
        self._discovery_binding_sidechannel = discovery_binding_sidechannel
        self._local_discovery_cache_authority = local_discovery_cache_authority
        self._fleet_catalog = fleet_catalog
        self._fleet_catalog_loaded_at = fleet_catalog_loaded_at
        self._tool_rows = tool_rows
        self._mounts = mounts
        self._load_catalog = load_catalog
        #: ONE semaphore for the instance's lifetime, so a probe still running from
        #: an earlier call counts against the same cap as a newer call's probes.
        self._probe_semaphore = asyncio.Semaphore(_PROBE_CONCURRENCY)
        #: The one local skill entry, built lazily and cached like the probe cache.
        self._local_skill_probe_cache: dict[str, _typing.Any] | None = None

    def _cache_probe(self, server_name: str, info: dict) -> dict:
        """Stamp ``probed_at`` (epoch seconds) and store ONE probe result.

        Every write to ``self._probe_cache`` goes through here so age/staleness
        can always be computed truthfully later — ``time.time() - probed_at`` —
        instead of a caller having to guess whether a served result is fresh
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        NE-008 (GOC-85 Deliverable 3): an OAuth-gated server's probe result is
        NEVER written into this shared, server-name-only cache — the result is
        bound to whichever principal happened to probe it, and this cache has
        no principal dimension. ``probed_at`` is still stamped on the returned
        ``info`` so the immediate caller (:meth:`probe_server`) can report a
        normal, honest result for THIS call; it is simply never reused for a
        later, possibly different, caller."""
        # A fresh probe replaces any prior private authority for this server,
        # including a failed OAuth probe.  A stale grant must never be reused
        # for a later catalog snapshot.
        self._drop_discovery_bindings_for_server(server_name)
        info["probed_at"] = time.time()
        cfg = self._load_catalog().get(server_name)
        if cfg is not None and oauth_gated(cfg):
            return info
        self._probe_cache[server_name] = self._tool_rows().resident(info)
        return info

    def _record_discovery_binding(
        self, server_name: str, info: dict[str, _typing.Any], binding: _typing.Any
    ) -> None:
        """Retain one typed binding for one exact probe result object.

        This is an internal hand-off only.  It intentionally accepts neither
        catalog metadata nor a caller-provided authority value; callers can
        only obtain a binding by completing a verified probe path above.
        Keeping the original object alongside its id makes id reuse harmless.
        """
        from arbora.fleet.discovery_authority import (
            OAuthGrantBinding,
            TenantLocalDiscoveryBinding,
        )

        if not isinstance(info, dict) or not isinstance(
            binding, (OAuthGrantBinding, TenantLocalDiscoveryBinding)
        ):
            return
        self._drop_discovery_bindings_for_server(server_name)
        self._discovery_binding_sidechannel[id(info)] = (server_name, info, binding)
        if isinstance(binding, TenantLocalDiscoveryBinding):
            self._local_discovery_cache_authority[id(info)] = (
                server_name,
                info,
                binding,
            )
        while len(self._discovery_binding_sidechannel) > 256:
            oldest = next(iter(self._discovery_binding_sidechannel))
            self._discovery_binding_sidechannel.pop(oldest, None)

    def _drop_discovery_bindings_for_server(self, server_name: str) -> None:
        """Forget private authority for a replaced/invalidated server probe."""
        for key, (recorded_server, _info, _binding) in tuple(
            self._discovery_binding_sidechannel.items()
        ):
            if recorded_server == server_name:
                self._discovery_binding_sidechannel.pop(key, None)
        for key, (recorded_server, _info, _binding) in tuple(
            self._local_discovery_cache_authority.items()
        ):
            if recorded_server == server_name:
                self._local_discovery_cache_authority.pop(key, None)

    def _rebound_cache_discovery_binding(
        self, server_name: _typing.Any, info: _typing.Any
    ) -> _typing.Any | None:
        """Re-mint local authority for an exact process-owned cached probe object.

        Non-OAuth results may be served from the process-owned cache after
        their prior side-channel record was consumed. Re-mint only for the
        exact cached object and a successful local probe that previously
        recorded verified provenance; copied/caller-shaped dictionaries never
        match.
        """
        cached_authority = self._local_discovery_cache_authority.get(id(info))
        if (
            cached_authority is None
            or cached_authority[0] != str(server_name)
            or cached_authority[1] is not info
        ):
            return None
        binding = _tenant_local_discovery_binding()
        if binding is None or binding.tenant_id != cached_authority[2].tenant_id:
            return None
        return binding

    def _take_discovery_bindings(
        self, catalog: _collections_abc.Mapping[str, _typing.Any]
    ) -> dict[str, _typing.Any]:
        """Consume private bindings for exact probe objects in ``catalog``.

        ``catalog`` is used only to identify which completed probe results the
        internal caller is syncing.  A copied or caller-shaped dictionary does
        not match the retained object identity, and any ``_discovery_binding``
        key in public metadata is ignored completely.
        """
        if not isinstance(catalog, _collections_abc.Mapping):
            return {}
        bindings: dict[str, _typing.Any] = {}
        for server_name, info in catalog.items():
            record = self._discovery_binding_sidechannel.get(id(info))
            if record is None:
                rebound = self._rebound_cache_discovery_binding(server_name, info)
                if rebound is not None:
                    bindings[str(server_name)] = rebound
                continue
            recorded_server, recorded_info, binding = record
            if recorded_server != str(server_name) or recorded_info is not info:
                continue
            bindings[str(server_name)] = binding
            self._discovery_binding_sidechannel.pop(id(info), None)
        return bindings

    def _bind_local_discovery_bindings(
        self, catalog: _collections_abc.Mapping[str, _typing.Any]
    ) -> None:
        """Bind exact local probe objects from the caller's verified context.

        ``source_sync`` may run the async multiplexer in a worker thread when
        its caller already owns an event loop; context variables do not cross
        that thread.  This explicit hand-off mints local authority back on the
        verified caller thread, but only for successful objects that are still
        the exact process-owned cache value.  It cannot authorize a copied or
        failed catalog result.
        """
        if not isinstance(catalog, _collections_abc.Mapping):
            return
        for server_name, info in catalog.items():
            if self._discovery_binding_sidechannel.get(id(info)) is not None:
                continue
            cached = self._probe_cache.get(str(server_name))
            cfg = self._load_catalog().get(str(server_name))
            if (
                cached is info
                and isinstance(cfg, _collections_abc.Mapping)
                and not oauth_gated(cfg)
                and isinstance(info, dict)
                and info.get("error") is None
            ):
                binding = _tenant_local_discovery_binding()
                if binding is not None:
                    self._record_discovery_binding(str(server_name), info, binding)

    def _probe_ttl(self) -> float:
        """The configured freshness window for a cached probe result
        (``MCP_CATALOG_PROBE_TTL`` / ``AgentConfig.mcp_catalog_probe_ttl``).
        Local import, same reason every other read of ``config`` in this
        module is local (avoid a module-load-time circular import with
        ``core.config``) (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness)."""
        return self._fleet.config.mcp_catalog_probe_ttl

    def _probe_cache_hit(
        self, server_name: str, now: float | None = None
    ) -> dict | None:
        """Return the cached probe for ``server_name`` iff it is still inside
        the TTL, else ``None`` — the ONE place that decides whether a cached
        entry may be served as-is, so :meth:`probe_server` (single-server
        short-circuit) and :meth:`probe_catalog` (whole-fleet re-probe
        targeting) can never disagree about what counts as fresh
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness).

        NE-008: always a miss for an OAuth-gated server (see
        :meth:`_cache_probe`) — there is never a cached entry to serve, so
        every call for one of these servers re-probes live, bound to
        whichever principal is asking THIS time."""
        cfg = self._load_catalog().get(server_name)
        if cfg is not None and oauth_gated(cfg):
            return None
        info = self._probe_cache.get(server_name)
        if info is None:
            return None
        if now is None:
            now = time.time()
        if (now - info.get("probed_at", now)) > self._probe_ttl():
            return None
        return info

    @staticmethod
    def _probe_age_and_staleness(
        info: dict, now: float, ttl: float
    ) -> tuple[float, bool]:
        """Honest ``(age_s, is_stale)`` for a served probe-cache entry
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness), computed
        from wall-clock age against ``ttl`` — never a bare echo of the
        narrow ``stale`` flag :meth:`probe_catalog` sets only when a result
        is recycled mid-call (that flag stays ``False`` forever on a
        normally-settled cache entry no matter how old, which is exactly how
        an 80-hour-old entry was once served as ``stale: false``). Still ORs
        in that narrower flag, since a just-recycled result is honestly "not
        this round's live answer" even inside the TTL window."""
        age_s = round(now - info.get("probed_at", now), 3)
        return age_s, bool(info.get("stale")) or age_s > ttl

    async def _live_child_probe(self, server_name: str) -> dict:
        """The probe answer for an ALREADY-MOUNTED child — read from its live
        session rather than paying a fresh connect."""
        session = self._live_primary_session(server_name)
        (
            resources,
            templates,
            native_prompts,
            skills,
            prompts,
            family_errors,
        ) = await self._probe_protocol_families(server_name, session)
        info: dict[str, _typing.Any] = {
            "tools": self._mounts()._live_tools_for_server(server_name),
            "resources": resources,
            "resource_templates": templates,
            "native_prompts": native_prompts,
            "catalog_family_errors": family_errors,
            "skills": skills,
            "prompts": prompts,
            "error": None,
        }
        result = self._cache_probe(server_name, info)
        self._record_discovery_binding(
            server_name, result, _tenant_local_discovery_binding()
        )
        return result

    async def _relay_read(
        self,
        server_name: str,
        read: _typing.Callable[[_typing.Any], _typing.Awaitable[_typing.Any]],
        timeout: float,
    ) -> _typing.Any:
        """Run one idempotent relay read on the child's live primary session.

        ARBORA-FLEET-R040.2: a child that restarted no longer knows the session.
        That failure tears the generation down, waits a bounded time for the
        supervisor to reconnect, and retries the read once on the new session.
        Any other failure, or a second dead session, propagates unchanged.
        """
        session = self._live_primary_session(server_name)
        try:
            return await asyncio.wait_for(read(session), timeout=timeout)
        except Exception as exc:
            runtime = self.children.get(server_name)
            if runtime is None or not _child_resilience.is_transient_child_death(exc):
                raise
            if not await runtime.recover("relay_read_dead_session"):
                raise
            logger.info(
                "MCP child %s session re-established for a relay read", server_name
            )
        session = self._live_primary_session(server_name)
        return await asyncio.wait_for(read(session), timeout=timeout)

    def _live_primary_session(self, server_name: str) -> _typing.Any:
        session = self.sessions.get(server_name)
        if session is None:
            raise RuntimeError("mounted child has no live primary session")
        return session

    async def probe_server(
        self, server_name: str, force: bool = False, timeout: float | None = None
    ) -> dict:
        """Probe ONE catalog server for its tool list: connect → list_tools →
        release (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). Returns (and caches) ``{"tools": [...],
        "error": str|None, "probed_at": float}``. An already-mounted child reuses
        its live tools instead of reconnecting; an unreachable server records its
        error string (so find_tools/load_tools can report *why* it is
        unavailable) rather than raising.

        A cache hit is honored only inside the TTL
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness,
        :meth:`_probe_cache_hit`) — an entry aged past
        ``mcp_catalog_probe_ttl`` is treated as a miss and re-probed here,
        same as ``force=True``, so a cache entry can never go stale forever."""
        if not force:
            cached = self._probe_cache_hit(server_name)
            if cached is not None:
                return cached

        if server_name in self.children:
            if force:
                await self._mounts()._relist_live_child(server_name)
            return await self._live_child_probe(server_name)

        cfg = self._load_catalog().get(server_name)
        if cfg is None:
            info: dict[str, _typing.Any] = {"tools": [], "error": "not in catalog"}
            return self._cache_probe(server_name, info)

        probe_to = _probe_timeout_seconds(cfg, timeout)
        if not 0.001 <= probe_to <= 300.0:
            info = {"tools": [], "error": "invalid probe timeout"}
            return self._cache_probe(server_name, info)

        async def _probe() -> tuple[
            list[dict],
            list[dict],
            list[dict],
            list[dict],
            list[dict],
            list[dict],
            dict[str, str],
            _typing.Any | None,
        ]:
            # Enter AND exit the transports within this single coroutine so the
            # anyio cancel scopes are not crossed between tasks. ``wait_for``
            # runs this whole coroutine as one task, so the stack is opened and
            # closed in the same task even on timeout-cancellation. (Wrapping
            # only the connect in wait_for would exit the scope in a different
            # task — "Attempted to exit cancel scope in a different task".)
            runtime_cfg, runtime_policy = _prepare_runtime_child_policy(
                cfg, self._fleet.config
            )
            binding_token = _CURRENT_DISCOVERY_BINDING.set(None)
            try:
                async with contextlib.AsyncExitStack() as stack:
                    session = await self.transport._open_one_session(
                        server_name, runtime_cfg, stack
                    )
                    tools = await _list_all_tools(session)
                    _bounded_tool_catalog(tools)
                    if force:
                        # An explicit refresh also re-onboards a changed
                        # catalog into the engine (ARBORA-FLEET-R070).
                        await self.transport._announce_handshake(
                            server_name, session, tools
                        )
                    if runtime_policy is not None:
                        tools = self.transport._admit_runtime_policy_tools(
                            server_name,
                            runtime_policy,
                            tools,
                        )
                    (
                        resources,
                        templates,
                        native_prompts,
                        skills,
                        prompts,
                        family_errors,
                    ) = await self._probe_protocol_families(
                        server_name,
                        session,
                    )
                    discovery_binding = _CURRENT_DISCOVERY_BINDING.get()
                    if discovery_binding is None:
                        discovery_binding = _tenant_local_discovery_binding()
                    return (
                        _bounded_tool_catalog(tools),
                        resources,
                        templates,
                        native_prompts,
                        skills,
                        prompts,
                        family_errors,
                        discovery_binding,
                    )
            finally:
                _CURRENT_DISCOVERY_BINDING.reset(binding_token)
                if runtime_policy is not None:
                    _close_runtime_child_policy(runtime_policy)
                    self._child_policy_admitted_tools.pop(server_name, None)

        info, discovery_binding = await _run_bounded_probe(_probe, probe_to)
        result = self._cache_probe(server_name, info)
        if discovery_binding is not None and info.get("error") is None:
            self._record_discovery_binding(server_name, result, discovery_binding)
        return result

    @staticmethod
    def _optional_method_missing(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in ("method not found", "not supported", "no such method")
        )

    async def _probe_protocol_families(
        self,
        server_name: str,
        session: _typing.Any,
    ) -> tuple[
        list[dict],
        list[dict],
        list[dict],
        list[dict],
        list[dict],
        dict[str, str],
    ]:
        """Read all native discovery families once from one child generation."""
        errors: dict[str, str] = {}
        try:
            raw_resources = await _list_all(session, "list_resources", "resources")
        except Exception as exc:
            logging.getLogger(__name__).debug("optional protocol method", exc_info=True)
            if not self._optional_method_missing(exc):
                errors["resources"] = type(exc).__name__
            raw_resources = []
        try:
            resources = _bounded_descriptor_catalog(
                raw_resources, key="uri", family="resource"
            )
            skills = _bounded_skill_catalog(raw_resources)
            prompt_resources = _bounded_prompt_catalog(raw_resources)
        except RuntimeError as exc:
            errors["resources"] = type(exc).__name__
            resources, skills, prompt_resources = [], [], []

        try:
            raw_templates = await _list_all(
                session, "list_resource_templates", "resource_templates"
            )
            templates = _bounded_descriptor_catalog(
                raw_templates,
                key="uriTemplate",
                family="resource-template",
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("optional protocol method", exc_info=True)
            if not self._optional_method_missing(exc):
                errors["resource_templates"] = type(exc).__name__
            templates = []

        try:
            native_prompts = _bounded_descriptor_catalog(
                await _list_all(session, "list_prompts", "prompts"),
                key="name",
                family="prompt",
            )
        except Exception as exc:
            logging.getLogger(__name__).debug("optional protocol method", exc_info=True)
            if not self._optional_method_missing(exc):
                errors["prompts"] = type(exc).__name__
            native_prompts = []

        self._fill_bodies_from_local_catalog(skills, "instructions")
        self._fill_bodies_from_local_catalog(prompt_resources, "body")
        return resources, templates, native_prompts, skills, prompt_resources, errors

    async def _probe_resource_family(
        self,
        server_name: str,
        session: _typing.Any,
        *,
        catalog_builder: _typing.Callable[[_typing.Any], list[dict]],
        kind: str,
        body_field: str,
    ) -> list[dict]:
        """Best-effort ``resources/list``-backed enumeration for one probed
        session, shared by ``_probe_skills`` and ``_probe_prompts``: both
        resource families degrade to an empty list, never a failed probe,
        when the session doesn't support resource discovery at all, or when
        this server's catalog for the family is malformed. Each entry's body
        comes from the resident local catalog only
        (``_fill_bodies_from_local_catalog``).
        """
        try:
            resources = await _list_all(session, "list_resources", "resources")
        except Exception as exc:  # resources/list is an OPTIONAL
            # MCP method; a server that doesn't implement it must still
            # contribute the tools/skills its probe already returned. The
            # cause IS logged so a real transport failure stays diagnosable.
            logger.debug(
                "Server %s does not support %s resource discovery: %s: %s",
                server_name,
                kind,
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
            return []
        try:
            entries = catalog_builder(resources)
        except Exception as exc:  # a malformed catalog from one
            # server must not fail the tool probe that already succeeded.
            # The cause IS logged.
            logger.warning(
                "Server %s returned an invalid %s resource catalog: %s: %s",
                server_name,
                kind,
                type(exc).__name__,
                redact_for_log(exc),
                exc_info=True,
            )
            return []
        self._fill_bodies_from_local_catalog(entries, body_field)
        return entries

    async def _probe_skills(
        self,
        server_name: str,
        session: _typing.Any,
    ) -> list[dict]:
        """Best-effort ``skill://`` resource enumeration for one probed session
        (CONCEPT:MERISTEM-ECO.mcp.skills-over-mcp-provider).

        Skills-over-MCP is only a fastmcp-4 server capability (draft MCP
        SEP-2640) — a fastmcp-3 or pre-skills server has no ``skill://``
        resources, and some MCP servers do not implement ``resources/list`` at
        all. Either case degrades to an empty list rather than failing the
        tool probe that already succeeded above.
        """
        return await self._probe_resource_family(
            server_name,
            session,
            catalog_builder=_bounded_skill_catalog,
            kind="skill",
            body_field="instructions",
        )

    def _fill_bodies_from_local_catalog(
        self, entries: list[dict], body_field: str
    ) -> None:
        """Fill each entry's body from the resident local-skill catalog.

        ARBORA-FLEET-R006.3.2: the catalog
        (``arbora.fleet.local_skill_catalog.build_local_skill_catalog``) is the
        only source of a skill or prompt body. A name it does not admit keeps
        no body; nothing reads a body back over the wire from the child.
        """
        try:
            local_entries = self._local_skill_probe_info().get("skills") or []
        except AttributeError:
            # A bare ``MCPMultiplexer.__new__(MCPMultiplexer)`` test fixture
            # never ran ``__init__`` and so has no ``_local_skill_probe_cache``
            # to build from; resolve afresh rather than crash.
            local_entries, _problems = build_local_skill_catalog()
        local_bodies = {
            item["name"]: item["instructions"]
            for item in local_entries
            if item.get("name") and item.get("instructions")
        }
        for entry in entries:
            local_body = local_bodies.get(entry.get("name"))
            if local_body is not None:
                entry[body_field] = local_body

    async def _probe_prompts(
        self,
        server_name: str,
        session: _typing.Any,
    ) -> list[dict]:
        """Best-effort ``prompt://`` resource enumeration for one probed
        session (CONCEPT:MERISTEM-ECO.mcp.cross-process-prompt-harvest).

        Prompts-over-MCP is served by every server built through
        ``server_factory.build_server`` (``_register_prompt_providers``), but
        a raw MCP child outside that factory — or one with no
        ``prompts/`` directory — has no ``prompt://`` resources; either case
        degrades to an empty list rather than failing the tool probe that
        already succeeded. A server that also doesn't implement
        ``resources/list`` at all degrades the same way.
        """
        return await self._probe_resource_family(
            server_name,
            session,
            catalog_builder=_bounded_prompt_catalog,
            kind="prompt",
            body_field="body",
        )

    def _settle_probe_task(self, server: str, task: asyncio.Task) -> None:
        """``add_done_callback`` for a background probe: release its in-flight
        slot. ``probe_server`` itself catches every failure mode it knows about
        and always caches a result, so an exception surfacing here is a
        genuinely unexpected bug, not a child's fault — it is logged with its
        cause, never swallowed, rather than left to asyncio's "exception was
        never retrieved" warning."""
        # Only retract the join slot if it is still OURS. A forced probe for the
        # same server runs as its OWN untracked task; popping unconditionally
        # would evict a DIFFERENT, still-running non-forced probe from the join
        # map, so a later call would spawn a duplicate of a probe already in
        # flight and ``aclose`` would no longer know to cancel it.
        if self._probe_inflight.get(server) is task:
            self._probe_inflight.pop(server, None)
        self._probe_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.error(
                "Unexpected exception from background probe of %s: %s: %s",
                server,
                type(exc).__name__,
                redact_for_log(exc),
            )

    def _ensure_probing(
        self, server: str, *, force: bool, timeout: float | None
    ) -> asyncio.Task:
        """Return the in-flight probe task for ``server``, joining one an
        overlapping call already started instead of spawning a duplicate.

        A forced probe (explicit re-probe, e.g. boot ingestion) always starts
        fresh and is never JOINED by a later caller — ``force`` means "I
        specifically want a new answer now," which a stale in-flight task
        cannot give. It is still registered in ``_probe_tasks`` so
        :meth:`aclose` can cancel it: "not shareable" must not mean "able to
        outlive the multiplexer".

        NE-008 (GOC-85 Deliverable 3): an OAuth-gated server ALWAYS runs as an
        untracked, un-joined task too, for the same reason a forced probe
        does — joining an in-flight task here would mean a SECOND principal's
        call returns the FIRST principal's probe result (the in-flight task's
        coroutine captured whichever actor was ambient when it was created via
        ``asyncio.create_task``), exactly the cross-principal catalog leak
        Deliverable 3 rules out. Never registered into ``_probe_inflight``
        either, so there is nothing for a later call to join."""
        cfg = self._load_catalog().get(server)
        oauth_gated_target = cfg is not None and oauth_gated(cfg)
        if not force and not oauth_gated_target:
            existing = self._probe_inflight.get(server)
            if existing is not None and not existing.done():
                return existing

        async def _run() -> dict:
            async with self._probe_semaphore:
                return await self.probe_server(server, force=force, timeout=timeout)

        task = asyncio.create_task(_run())
        self._probe_tasks.add(task)
        if not force and not oauth_gated_target:
            self._probe_inflight[server] = task
        # `server` is this call's own parameter (not a mutating loop variable), so a
        # plain closure captures it correctly without the default-arg-capture trick —
        # which also lets mypy infer the callback's type against
        # ``Task.add_done_callback``'s single-argument signature.
        task.add_done_callback(lambda t: self._settle_probe_task(server, t))
        return task

    def _probe_targets(
        self,
        catalog: dict[str, dict],
        servers: list[str] | tuple[str, ...] | None,
        force: bool,
    ) -> list[str]:
        """Which catalog servers this call must (re-)probe.

        A cached entry aged past ``mcp_catalog_probe_ttl`` is targeted exactly
        like an uncached one (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness).
        """
        candidates = (
            catalog if servers is None else (s for s in servers if s in catalog)
        )
        return [
            server
            for server in candidates
            if force or self._probe_cache_hit(server) is None
        ]

    def _spawn_probe_tasks(
        self,
        targets: list[str],
        force: bool,
        timeout: float | None,
        priority: _resource_priority.PriorityClass | None,
    ) -> dict[asyncio.Task, str]:
        """Start (or join) one probe per target under this call's priority scope."""
        scope = (
            _resource_priority.priority_scope(priority)
            if priority is not None
            else contextlib.nullcontext()
        )
        with scope:
            return {
                self._ensure_probing(server, force=force, timeout=timeout): server
                for server in targets
            }

    def _settled_probe_results(
        self,
        tasks: dict[asyncio.Task, str],
        pending: frozenset[asyncio.Task] | set[asyncio.Task] = frozenset(),
    ) -> dict[str, dict]:
        """Fold every SETTLED probe task's answer over the cached fleet view.

        A task that is still running, was cancelled, or raised contributes
        nothing — one unreachable server can never take the sweep down with it.
        """
        result = dict(self._probe_cache)
        for task, server in tasks.items():
            if task in pending or task.cancelled() or not task.done():
                continue
            try:
                info = task.result()
            except BaseException:
                logging.getLogger(__name__).debug(
                    "one server never fails the sweep", exc_info=True
                )
                continue
            if isinstance(info, dict):
                result[server] = info
        return result

    def _unsettled_probe_entry(self, server: str, now: float, budget: float) -> dict:
        """The honest answer for a server still probing when the budget expired.

        Reachable two ways: an explicit force re-probe, or a TTL-expired cache
        entry (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness)
        re-targeted by ``_probe_targets`` — either way, serve the last known
        answer rather than a bare "unavailable", labelled so the caller knows
        it is not this round's live result. With no prior answer at all, say
        so explicitly rather than implying the server is unreachable.
        """
        prior = self._probe_cache.get(server)
        if prior is None:
            return {
                "tools": [],
                "error": (
                    f"still probing after {budget:g}s (no result yet) — "
                    "the probe continues in the background; call again shortly"
                ),
                "pending": True,
            }
        stale = dict(prior)
        stale["stale"] = True
        stale["age_s"] = round(now - prior.get("probed_at", now), 3)
        return stale

    async def probe_catalog(
        self,
        force: bool = False,
        timeout: float | None = None,
        budget: float | None = None,
        servers: list[str] | tuple[str, ...] | None = None,
        priority: _resource_priority.PriorityClass | None = None,
    ) -> dict[str, dict]:
        """Probe every catalog server concurrently (bounded) and cache the
        result, so find_tools can rank the whole fleet's real tools.

        ``budget`` bounds how long THIS CALL waits for an answer. It does
        **not** bound how long an individual probe is allowed to run — that
        is each server's own ``probe_timeout``/``timeout``, honored
        independently by :meth:`probe_server`. A server still probing when
        the budget expires is NEVER cancelled: cancelling it would also
        cancel its own ``self._probe_cache`` write, which is exactly what
        used to make an unreachable server pay its full connect cost again
        on every subsequent call, forever, instead of the cache the tool's
        own contract promises ("the first call probes the fleet; later
        calls are cached"). It keeps running toward its own deadline in the
        background — ``_probe_inflight`` lets a later call join that SAME
        probe instead of duplicating it — and each pending server gets an
        honest, individual answer here: a prior (possibly ``stale``,
        age-labelled) result if one exists, or an explicit ``pending``
        marker on its first-ever attempt. Reachable servers are never
        blocked by an unreachable one (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).

        A long-lived multiplexer (arbora's fleet loader) relies on
        :meth:`aclose` to cancel whatever is still in-flight at real
        shutdown. A short-lived caller wrapped in ``asyncio.run(...)``
        (e.g. ``source_sync._sync_fleet`` via ``_run_async``) gets the same
        guarantee for free — ``asyncio.run`` cancels every task still on its
        loop when the coroutine it ran returns, this multiplexer's included
        — so no caller needs to await these background probes itself.

        ``priority`` tags this call's probe tasks with a
        :class:`_resource_priority.PriorityClass` (ORCH-1.98) for their whole background
        lifetime — pass ``_resource_priority.PriorityClass.BACKGROUND_INGESTION`` for a broad
        catalog sweep so it yields shared-resource contention to
        interactive/orchestration work; leave unset to inherit the
        caller's ambient priority (the default — appropriate for a small,
        caller-named set of servers on the interactive path).

        ``servers`` narrows a latency-sensitive first stage without
        creating a second probe path.

        A cached entry aged past ``mcp_catalog_probe_ttl`` is targeted for
        re-probe exactly like an uncached one
        (CONCEPT:MERISTEM-ECO.multiplexer.catalog-probe-ttl-staleness) — the fleet
        cache never goes stale forever. The re-probe runs through the same
        ``_ensure_probing``/budget machinery as any other target, so it
        NEVER blocks this call past its own ``budget``: a caller that hits
        the deadline before the refresh lands still gets the last-known
        answer, honestly labelled ``stale`` with its real ``age_s``, while
        the refresh keeps running in the background for the next call."""
        catalog = self._load_catalog()
        targets = self._probe_targets(catalog, servers, force)
        if not targets:
            return self._probe_cache

        if budget is not None and not 0.001 <= budget <= 300.0:
            raise ValueError("catalog probe budget is outside the safety boundary")

        tasks = self._spawn_probe_tasks(targets, force, timeout, priority)

        if budget is None:
            await asyncio.gather(*tasks, return_exceptions=True)
            return self._settled_probe_results(tasks)

        _done, pending = await asyncio.wait(tasks, timeout=budget)
        result = self._settled_probe_results(tasks, pending)
        if pending:
            now = time.time()
            for task in pending:
                server = tasks[task]
                result[server] = self._unsettled_probe_entry(server, now, budget)
        return result

    def _server_level_fallback(self) -> list[dict]:
        """When the KG yields no tool-level index (cold/absent KG), still let
        the caller act: surface mountable servers so they can ``load_tools`` by
        server name."""
        out: list[dict] = []
        for server in self._load_catalog():
            out.append(
                {
                    "server": server,
                    "tool": "*",
                    "prefixed_name": None,
                    "description": (
                        f"All tools for '{server}'. KG tool-level discovery "
                        "is unavailable; load the whole server by name."
                    ),
                    "score": 0.0,
                    "mountable": True,
                    "mounted": server in self.children,
                }
            )
        return out

    @staticmethod
    def _priority_catalog_servers(
        query: str, catalog: _collections_abc.Mapping[str, _typing.Any]
    ) -> list[str]:
        """Return high-confidence server-name matches for staged discovery."""

        query_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        ranked: list[tuple[float, int, str]] = []
        for server in catalog:
            identity_tokens = set(re.findall(r"[a-z0-9]+", server.lower()))
            identity_tokens -= _SERVER_DISCOVERY_STOPWORDS
            if not identity_tokens:
                continue
            overlap = query_tokens & identity_tokens
            coverage = len(overlap) / len(identity_tokens)
            if overlap and coverage >= 0.5:
                ranked.append((coverage, len(overlap), server))
        ranked.sort(reverse=True)
        return [server for _coverage, _overlap, server in ranked]

    async def _discovery_probe(
        self,
        query: str,
        catalog: dict[str, dict],
        discovery_timeout: float,
        deadline: float,
    ) -> dict:
        """The probe view backing one ``find_tools`` call, within its budget.

        A latency-sensitive first stage probes only the query-relevant servers.
        The broad fleet-wide sweep that follows (every server, not just those)
        is a background warm-up, not itself the interactive answer — tag it
        BACKGROUND_INGESTION (ORCH-1.98) so it yields shared-resource
        contention to interactive/orchestration work instead of competing with
        it, reusing the ONE existing priority gate rather than inventing a
        second (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog).
        """
        loop = asyncio.get_running_loop()
        probe: dict = {}
        priority_servers = self._priority_catalog_servers(query, catalog)
        if priority_servers:
            probe = await self.probe_catalog(
                budget=discovery_timeout,
                servers=priority_servers,
            )
        remaining = deadline - loop.time()
        if remaining >= 0.001:
            probe = await self.probe_catalog(
                budget=remaining,
                priority=_resource_priority.PriorityClass.BACKGROUND_INGESTION,
            )
        return probe

    def _catalog_tool_probe_info(self) -> dict[str, dict[str, _typing.Any]]:
        """Per-server tool descriptors already admitted into the EG catalog
        snapshot (ARBORA-FLEET-R031), read with NO live probe.

        Fleet onboarding (``arbora/fleet/onboarding.py``) imports each
        admitted server's content pack — its tools included — into the EG
        catalog before any child is ever mounted
        (CONCEPT:MERISTEM-ECO.multiplexer.tool-gateway-catalog). This reads that
        already-imported ``kind == "tool"`` provenance straight off
        ``self._fleet_catalog()`` (installed by :meth:`refresh_engine_catalog`)
        so a server whose live probe cannot answer in time still has
        descriptors ``discover_tools`` can rank — the live probe stays the
        freshness refresh, never the only source. Every row is stamped
        ``stale: True`` and ages from the snapshot's own load time, never
        faked as a live answer.
        """
        catalog = self._fleet_catalog()
        if catalog is None:
            return {}
        probed_at = self._fleet_catalog_loaded_at() or 0.0
        out: dict[str, dict[str, _typing.Any]] = {}
        for server in catalog.servers:
            tools = [
                {"name": item.entry.upstream_name, "description": item.entry.summary}
                for item in server.provides
                if item.entry.kind == "tool" and item.entry.upstream_name
            ]
            if not tools:
                continue
            out[server.component.server_name] = {
                "tools": tools,
                "skills": [],
                "error": None,
                "stale": True,
                "probed_at": probed_at,
                "source": "catalog_snapshot",
            }
        return out

    def _local_skill_probe_info(self) -> dict[str, _typing.Any]:
        """Build (once) and cache the local-skill pseudo-server probe entry.

        "Local" means every skill ``arbora.core.providers.
        resolve_skill_provider_dirs()`` resolves in THIS process — arbora's
        own skills, meristem's own, and any sibling package installed
        alongside arbora that declares ``scion_sdk.connector``
        (CONCEPT:MERISTEM-KG.retrieval.unified-capability-contract). An
        individually unreadable/malformed skill, and a core-pack name no
        installed provider resolves, are reported loudly here (never fatal,
        never silent) the one time this cache is built.
        """
        if self._local_skill_probe_cache is not None:
            return self._local_skill_probe_cache
        entries, problems = build_local_skill_catalog()
        for problem in problems:
            logger.warning("Local skill catalog: %s", problem)
        for missing in missing_core_pack_names(entries):
            logger.warning(
                "Core-pack skill %r is not provided by any installed package",
                missing,
            )
        info: dict[str, _typing.Any] = {"tools": [], "skills": entries, "error": None}
        self._local_skill_probe_cache = info
        return info
