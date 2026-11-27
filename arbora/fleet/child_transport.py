"""Child spawn, environment, remote validation and transport opening (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import asyncio
import collections.abc as _collections_abc
import contextlib
import contextvars
import logging
import os
import re
import typing as _typing
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import mcp as _mcp
from mcp.client.session import ClientSession, IncomingMessage, MessageHandlerFnT

from arbora.core.environment import inherited_environment, read_reference
from arbora.core.settings import RuntimeConfig
from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    from mcp_types import Tool as MCPTool
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from arbora.fleet.child_payload_bounds import (
    _MAX_DELEGATED_VALUE_BYTES,
    _bounded_tool_catalog,
    _list_all_tools,
)
from arbora.fleet.child_runtime_policy import (
    _ENV_TEMPLATE_RE,
    _PROVIDER_CHILD_ENV_KEYS,
    _RUNTIME_CHILD_POLICY_INTERNAL_KEY,
    _RUNTIME_REF_RE,
    _runtime_materialized,
    _selected_child_provider_profile,
    _sensitive_config_key,
)
from arbora.fleet.oauth_brokers import oauth_gated
from arbora.security.log_redaction import redact_for_log

logger = logging.getLogger("mcp_multiplexer")
# The SOLE handle on the MCP protocol types in this module, for two reasons that
# both bite here. (1) MCP SDK v2 re-homed the whole `mcp.types` namespace into the
# standalone `mcp_types` distribution, so `import mcp.types` raises ImportError at
# module scope on an SDK v2 image — and this module is the fleet loader, so that
# takes every child server down with it. `mcp_types_module()` binds whichever the
# installed SDK ships. (2) `mcp` is also a common PARAMETER name in this file (see
# `_register_forwarder`), where it shadows the package outright; a `mcp_types.X`
# attribute chain there resolves against the parameter, not the SDK.
mcp_types = mcp_types_module()


# Public transport seams are kept as module attributes so tests and embedders can
# replace a transport without patching the third-party package globally.
StdioServerParameters = _mcp.StdioServerParameters


stdio_client = _mcp.stdio_client


_HTTP_TE_HEADER = "".join(("t", "e"))


#: The largest single SSE event arbora reads from a streamable-http child
#: (ARBORA-OPS-R020.6.8). One event carries one whole JSON-RPC result, whose
#: text content is the tool's JSON escaped once more, so the bound is twice the
#: delegated value bound. The MCP client default (1 MiB) is below arbora's own
#: value bound and failed every 50-record ServiceNow page.
_MAX_CHILD_SSE_EVENT_BYTES = 2 * _MAX_DELEGATED_VALUE_BYTES


_ENV_RUNTIME_REF_RE = re.compile(r"^env://([A-Z][A-Z0-9_]{0,127})$")


_STORE_RUNTIME_REF_RE = re.compile(
    r"^(?:vault|secret)://[A-Za-z0-9][A-Za-z0-9_./#-]{0,511}$"
)


_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "ARBORA_CONFIG_DIR",
        "COMSPEC",
        "APPDATA",
        "HOME",
        "LANG",
        "LC_ALL",
        "LOCALAPPDATA",
        "NO_PROXY",
        "PATH",
        "PATHEXT",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "USERPROFILE",
        "UV_NATIVE_TLS",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
        "XDG_STATE_HOME",
    }
)


_TASK_DELEGATION_CHANNEL_ENV = "ARBORA_MCP_TASK_CHANNEL_SECRET"


def _resolve_fleet_runtime_reference(reference: str, config: RuntimeConfig) -> str:
    """Resolve one fleet reference with direct aliases taking precedence.

    ``env://ALIAS`` first reads the live environment projection, which includes
    the runtime-secrets source. Only an unavailable direct alias consults the
    validated ``AgentConfig.mcp_fleet_secret_refs`` mapping. Store references
    resolve through the central runtime secret resolver. No resolved value is
    retained in the catalog or configuration model.
    """

    env_match = _ENV_RUNTIME_REF_RE.fullmatch(reference)
    if env_match is not None:
        alias = env_match.group(1)
        direct = read_reference(alias, None)
        if direct not in (None, ""):
            return str(direct)

        mappings = getattr(config, "mcp_fleet_secret_refs", None)
        if not isinstance(mappings, dict):
            raise RuntimeError("MCP fleet secret alias mapping is invalid")
        mapped_reference = mappings.get(alias)
        if mapped_reference in (None, ""):
            raise RuntimeError("MCP child runtime reference is unavailable")
        reference = str(mapped_reference)
    elif _STORE_RUNTIME_REF_RE.fullmatch(reference) is None:
        raise RuntimeError("MCP child runtime reference is invalid")

    from scion_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    try:
        return resolve_runtime_secret_reference(reference)
    except Exception:
        logger.warning("_resolve_fleet_runtime_reference failed", exc_info=True)
        raise RuntimeError("MCP child runtime reference is unavailable") from None


def resolve_runtime_value(
    value: _typing.Any,
    *,
    sensitive: bool,
    config: RuntimeConfig,
    materialized: bool = False,
) -> str:
    """Resolve externalized catalog values without ever expanding the whole file."""
    rendered = str(value or "")
    if _RUNTIME_REF_RE.fullmatch(rendered):
        rendered = _resolve_fleet_runtime_reference(rendered, config)
    elif rendered.startswith(("env://", "vault://", "secret://")):
        raise RuntimeError("MCP child runtime reference is invalid")
    else:
        had_template = bool(_ENV_TEMPLATE_RE.search(rendered))

        def replace(match: re.Match[str]) -> str:

            variable = match.group(1)
            if not sensitive and _sensitive_config_key(variable):
                raise RuntimeError(
                    "Sensitive MCP runtime values require a credential field"
                )
            resolved = read_reference(variable, None)
            if resolved in (None, ""):
                raise RuntimeError("MCP child environment reference is unavailable")
            return str(resolved)

        rendered = _ENV_TEMPLATE_RE.sub(replace, rendered)
        if sensitive and not (materialized or had_template):
            raise RuntimeError("MCP child credentials must use runtime references")
    if "\x00" in rendered or "\r" in rendered or "\n" in rendered:
        raise RuntimeError("MCP child runtime value is invalid")
    return rendered


def _child_transport_values(
    cfg: dict, command: _typing.Any, config: RuntimeConfig
) -> tuple[_typing.Any, str, str, bool | None]:
    """Resolve child transport fields; ``None`` marks an invalid declaration."""
    url = resolve_runtime_value(cfg.get("url", ""), sensitive=False, config=config)
    explicit_transport = str(cfg.get("transport", "")).lower()
    invalid = (
        explicit_transport not in {"", "streamable-http", "sse"}
        or bool(command) == bool(url)
        or (explicit_transport and not url)
    )
    if invalid:
        return command, url, explicit_transport, None
    is_remote = bool(url) or explicit_transport in ("streamable-http", "sse")
    return command, url, explicit_transport, is_remote


# ---------------------------------------------------------------------------
# NE-008 (GOC-85 Deliverable 3): per-principal remote-OAuth wiring.
#
# A catalog entry opts into per-user delegated authorization by declaring an
# admin-configured ``oauth_provider`` block (the exact shape
# ``arbora.fleet.remote_oauth_broker.ProviderDescriptor`` accepts) on
# its config -- the SAME "administrator-populated, never caller-supplied"
# catalog these config dicts already are (headers/env/tls_profile/
# allowed_private_hosts all come from here today). Such a server is
# structurally excluded from the fleet-global, principal-agnostic pool
# (``_start_child`` skips it, see below) and from the shared probe-result
# cache (``_probe_cache_hit``/``_cache_probe``, see below) -- it is served
# EXCLUSIVELY through ephemeral, per-request sessions
# (``_open_one_session``, already used this way by ``probe_server`` for any
# un-pooled server), each one bound to the CURRENT caller's own delegated
# grant. This deliberately reuses the broker's own reserved seam
# (``bearer_headers_for``) and this repo's existing verified-actor primitive
# (``brain_context.current_actor``) rather than any of the specific outbound
# delegated-identity primitive names the U-44/U-45 canary in
# ``tests/unit/mcp/test_remote_oauth_fail_closed.py`` fences (see that test's
# own module docstring for the list) -- those would mean a per-user token
# reached the ONE shared session-per-server pool every other caller also
# uses, which this wiring never does.
# ---------------------------------------------------------------------------
_CURRENT_DISCOVERY_BINDING: contextvars.ContextVar[_typing.Any | None] = (
    contextvars.ContextVar("current_discovery_binding", default=None)
)


class ChildTransport:
    """Opens one child session: the launch environment, remote-url and header validation, TLS trust, and the stdio or remote transport."""

    def __init__(
        self,
        *,
        child_catalog_fingerprints: _typing.Any,
        child_policy_admitted_tools: _typing.Any,
        fleet: _typing.Any,
        list_changed_tasks: _typing.Any,
        probe_cache: _typing.Any,
        publish_live_tools: _typing.Any,
    ) -> None:
        self._child_catalog_fingerprints = child_catalog_fingerprints
        self._child_policy_admitted_tools = child_policy_admitted_tools
        self._fleet = fleet
        self._list_changed_tasks = list_changed_tasks
        self._probe_cache = probe_cache
        self._publish_live_tools = publish_live_tools
        # Called once per completed child handshake (ARBORA-FLEET-R041.7).
        self._handshake_listener: _typing.Callable[..., None] | None = None

    def _admit_runtime_policy_tools(
        self,
        server_name: str,
        policy: _typing.Any,
        tools: list[MCPTool],
        *,
        record_state: bool = True,
    ) -> list[MCPTool]:
        """Apply live catalog admission and fingerprinting fail closed."""

        catalog = [
            {
                "annotations": getattr(tool, "annotations", None),
                "inputSchema": tool.input_schema,
                "name": tool.name,
            }
            for tool in tools
        ]
        try:
            fingerprint = policy.fingerprint_catalog(catalog)
            if not isinstance(fingerprint, str) or not re.fullmatch(
                r"[0-9a-f]{64}", fingerprint
            ):
                raise RuntimeError("invalid fingerprint")
            admitted = [
                tool
                for tool in tools
                if policy.allows_tool(tool.name, getattr(tool, "annotations", None))
            ]
        except Exception:
            logger.warning("_admit_runtime_policy_tools failed", exc_info=True)
            raise RuntimeError(
                "MCP child runtime policy rejected live catalog"
            ) from None
        if record_state:
            self._child_catalog_fingerprints[server_name] = fingerprint
            self._child_policy_admitted_tools[server_name] = frozenset(
                tool.name for tool in admitted
            )
        return admitted

    def _resolve_transport_kind_for_child(
        self, server_name: str, cfg: dict
    ) -> tuple[str, str, str, bool]:
        """``(command, url, explicit_transport, is_remote)``.

        ``command`` is ``""`` exactly when ``is_remote`` — the guard below
        refuses a local child with no command, so a non-remote result always
        carries a real one. Returning ``""`` rather than ``None`` for the
        remote case states that in the type: the sole caller passes ``command``
        only into the local branch.
        """
        command, url, explicit_transport, is_remote = _child_transport_values(
            cfg, self._child_command(cfg), self._fleet.config
        )
        if is_remote is None:
            raise RuntimeError("MCP child transport declaration is invalid")
        if not command and not is_remote:
            raise RuntimeError("MCP child requires a command or URL")
        if not is_remote:
            if self._fleet.config.mcp_stdio_prohibited:
                raise RuntimeError(
                    "stdio transport is not permitted in this process "
                    f"(server={server_name!r}): MCP_STDIO_PROHIBITED is set"
                )
        return command, url, explicit_transport, is_remote

    @staticmethod
    def _child_command(cfg: dict) -> str:
        """The configured stdio command, or ``""`` for a URL-only child."""
        return str(cfg.get("command") or "")

    @staticmethod
    def _resolve_child_initialization_timeout(cfg: dict) -> float:
        try:
            initialization_timeout = float(
                cfg.get("initialization_timeout", cfg.get("timeout", 300.0))
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError("MCP child initialization timeout is invalid") from exc
        if not 0.001 <= initialization_timeout <= 3_600.0:
            raise RuntimeError("MCP child initialization timeout is invalid")
        return initialization_timeout

    @staticmethod
    def _runtime_policy_environment(runtime_policy: _typing.Any) -> dict[str, str]:
        try:
            policy_environment = runtime_policy.child_environment()
        except Exception:
            logger.warning("_runtime_policy_environment failed", exc_info=True)
            raise RuntimeError(
                "MCP child runtime policy environment is unavailable"
            ) from None
        if (
            not isinstance(policy_environment, _collections_abc.Mapping)
            or len(policy_environment) > 256
        ):
            raise RuntimeError("MCP child runtime policy environment is invalid")
        provider_environment: dict[str, str] = {}
        for raw_key, raw_value in policy_environment.items():
            key = str(raw_key)
            if (
                not isinstance(raw_key, str)
                or not isinstance(raw_value, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
                or len(raw_value.encode("utf-8")) > 65_536
                or "\x00" in raw_value
            ):
                raise RuntimeError("MCP child runtime policy environment is invalid")
            provider_environment[key] = raw_value
        return provider_environment

    @staticmethod
    async def _resolve_provider_profile_environment(
        provider_profile: _typing.Any,
        stack: contextlib.AsyncExitStack,
        initialization_timeout: float,
    ) -> dict[str, str]:
        """Refuse a child that selects a provider profile.

        Arbora serves no provider runtime profiles, so a child that names one
        fails closed before its process starts.
        """
        del stack, initialization_timeout
        raise RuntimeError(
            f"MCP child provider profile {provider_profile!r} is not served by this host"
        )

    async def _resolve_child_provider_environment(
        self,
        cfg: dict,
        is_remote: bool,
        stack: contextlib.AsyncExitStack,
        initialization_timeout: float,
    ) -> tuple[dict[str, str], _typing.Any]:
        """Returns (provider_environment, runtime_policy). ``provider_environment``
        is populated from the runtime policy, then REPLACED (not merged) by the
        provider profile's environment if a profile is also configured — matching
        the original code's exact (possibly surprising) precedence."""
        provider_profile = _selected_child_provider_profile(cfg, is_remote=is_remote)
        runtime_policy = cfg.get(_RUNTIME_CHILD_POLICY_INTERNAL_KEY)
        provider_environment: dict[str, str] = {}
        if runtime_policy is not None:
            provider_environment = self._runtime_policy_environment(runtime_policy)
        if provider_profile is not None:
            provider_environment = await self._resolve_provider_profile_environment(
                provider_profile, stack, initialization_timeout
            )
        return provider_environment, runtime_policy

    @staticmethod
    def _validate_remote_child_url(url: str) -> _typing.Any:
        parsed_url = urlsplit(url)
        if (
            parsed_url.scheme.lower() not in {"http", "https"}
            or not parsed_url.hostname
            or parsed_url.username is not None
            or parsed_url.password is not None
            or parsed_url.fragment
            or parsed_url.query
            or len(url) > 8_192
        ):
            raise RuntimeError("Remote MCP child URL is invalid")
        return parsed_url

    @staticmethod
    def _resolve_remote_allowed_private_hosts(
        cfg: dict, parsed_url: _typing.Any, url: str, config: RuntimeConfig
    ) -> list[str]:
        # Computed here (not just at the transport-pinning site below) so the
        # scheme gate consults the SAME allowlist as the actual DNS-pinned
        # egress: MCP_HTTP_ALLOWED_PRIVATE_HOSTS was already a config field
        # for exactly this (mirroring OIDC_HTTP_ALLOWED_PRIVATE_HOSTS /
        # MODEL_HTTP_ALLOWED_PRIVATE_HOSTS), but this gate never read it —
        # so a deployment that legitimately reaches its MCP fleet over
        # plain HTTP behind a TLS-terminating ingress (MCP_TLS_TERMINATED)
        # could never declare that trust; it always hard-failed here first.
        child_private_hosts = cfg.get("allowed_private_hosts", [])
        if not isinstance(child_private_hosts, list):
            raise RuntimeError("Remote MCP child private-host policy is invalid")
        allowed_private_hosts = [
            *config.mcp_http_allowed_private_hosts,
            *(str(value) for value in child_private_hosts),
        ]
        if parsed_url.scheme.lower() == "http" and parsed_url.hostname.lower() not in {
            "localhost",
            "127.0.0.1",
            "::1",
            *(host.lower() for host in allowed_private_hosts),
        }:
            raise RuntimeError("Remote MCP child requires HTTPS outside loopback")
        return allowed_private_hosts

    @staticmethod
    def _validate_one_remote_header(
        name: str, value: str, materialized: set[str], config: RuntimeConfig
    ) -> str:
        lowered = name.lower()
        if lowered in {
            "connection",
            "content-length",
            "host",
            "proxy-connection",
            _HTTP_TE_HEADER,
            "trailer",
            "transfer-encoding",
            "upgrade",
        }:
            raise RuntimeError("Remote MCP child headers are invalid")
        rendered = resolve_runtime_value(
            value,
            sensitive=_sensitive_config_key(name),
            materialized=name in materialized,
            config=config,
        )
        if (
            not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", name)
            or len(rendered) > 16_384
            or "\r" in rendered
            or "\n" in rendered
        ):
            raise RuntimeError("Remote MCP child headers are invalid")
        return rendered

    @staticmethod
    def _validate_remote_child_headers(
        cfg: dict, config: RuntimeConfig
    ) -> dict[str, str] | None:
        headers = cfg.get("headers")
        if not headers:
            return headers
        if not isinstance(headers, dict) or len(headers) > 64:
            raise RuntimeError("Remote MCP child headers are invalid")
        materialized = (
            set(cfg.get("_runtime_materialized_secret_keys") or [])
            if _runtime_materialized(cfg)
            else set()
        )
        validated_headers: dict[str, str] = {}
        for key, value in headers.items():
            name = str(key)
            validated_headers[name] = ChildTransport._validate_one_remote_header(
                name, value, materialized, config
            )
        return validated_headers

    async def _resolve_actor_exchange_bearer(self, cfg: dict) -> dict[str, str] | None:
        """The one exchange call site of delegated identity (ARBORA-IDENTITY-R028.3).

        An actor-required tool (``requires_actor`` from ``tools/list`` ``_meta``) gets an
        exchanged bearer for the verified acting user; a refusal makes no connector call
        and a process credential never stands in.
        """
        from arbora.access.delegation import resolve_actor_exchange_bearer
        from arbora.security.actor_context import current_actor
        from arbora.security.session import current_session

        needs = cfg.get("requires_actor") is True
        if not needs:
            return None
        session = current_session()
        actor = current_actor() if session is not None else None
        caller = None
        claims = None
        if actor is not None and session is not None:
            caller = SimpleNamespace(
                principal=actor.actor_id,
                tenant=actor.tenant_id,
                principal_kind="user"
                if actor.actor_type.value == "human"
                else "service",
                authenticated=actor.authenticated,
                effective_scopes=frozenset(getattr(session, "scopes", ()) or ()),
                request_id="",
            )
            claims = getattr(self._fleet, "actor_claims", lambda _a: None)(actor)
        return await resolve_actor_exchange_bearer(
            str(cfg.get("name", "")),
            str(cfg.get("tool", "")),
            needs_actor=needs,
            caller=caller,
            claims=claims,
            connector_client_id=str(cfg.get("client_id") or cfg.get("name") or ""),
            issuer=getattr(self._fleet, "actor_issuer", None),
            emit=getattr(self._fleet, "actor_audit", lambda _event: None),
        )

    async def _reused_credential_headers(self, cfg: dict, url: str) -> dict[str, str]:
        """The one production caller of scoped credential reuse (ARBORA-IDENTITY-R023.2).

        The verified caller reuses the declared owner's provider credential only under an
        engine grant for this agent, connector and purpose; a refusal opens no session.
        """
        from arbora.security.actor_context import current_actor

        actor = current_actor()
        caller = SimpleNamespace(
            tenant=actor.tenant_id,
            principal=actor.actor_id,
            client=actor.actor_id,
        )
        from arbora.fleet.remote_oauth_tokens import ProviderDescriptor

        descriptor = ProviderDescriptor(**cfg["oauth_provider"])
        return await self._fleet.credentials.bearer(caller, cfg, url, descriptor)

    async def _apply_remote_child_oauth_grant(
        self, cfg: dict, url: str, headers: dict[str, str] | None
    ) -> tuple[dict[str, str] | None, dict[str, str] | None]:
        """A reused credential of a declared owner, else the caller's own grant."""
        if not (cfg.get("credential_owner") and oauth_gated(cfg)):
            return await self._apply_caller_oauth_grant(cfg, url, headers)
        reused = await self._reused_credential_headers(cfg, url)
        return {**(headers or {}), **reused}, reused

    async def _apply_caller_oauth_grant(
        self, cfg: dict, url: str, headers: dict[str, str] | None
    ) -> tuple[dict[str, str] | None, dict[str, str] | None]:
        # NE-008 (GOC-85 Deliverable 3): per-principal remote-OAuth bearer,
        # for a child catalog entry that declares ``oauth_provider``.
        # ``None`` for every other (unchanged) child. Offloaded to a thread
        # -- secrets-backend I/O is synchronous. Raises fail-closed
        # (missing/expired/revoked grant) rather than falling back to any
        # shared/service credential; the caller sees that failure as this
        # session never opening, exactly like any other connect failure.
        actor_headers = await self._resolve_actor_exchange_bearer(cfg)
        if actor_headers is not None:
            headers = {**(headers or {}), **actor_headers}
        oauth_grant = await asyncio.to_thread(self._fleet.oauth_brokers.grant, cfg, url)
        if oauth_grant is not None:
            oauth_bearer_headers, discovery_binding = oauth_grant
            _CURRENT_DISCOVERY_BINDING.set(discovery_binding)
            headers = {**(headers or {}), **oauth_bearer_headers}
        else:
            oauth_bearer_headers = None
        return headers, oauth_bearer_headers

    @staticmethod
    def _resolve_remote_child_tls_trust(
        cfg: dict, stack: contextlib.AsyncExitStack, config: RuntimeConfig
    ) -> _typing.Any:
        from arbora.core.transport import (
            resolve_configured_tls_profile,
        )

        profile_name = str(cfg.get("tls_profile") or "").strip() or None
        profile_ref = str(cfg.get("tls_profile_ref") or "").strip() or None
        trust = resolve_configured_tls_profile(
            "MCP_CHILD",
            profile_name=profile_name,
            profile_ref=profile_ref,
            config=config,
        )
        stack.callback(trust.cleanup)
        if trust.proxy_url:
            raise RuntimeError("Remote MCP child cannot use an inline proxy")
        return trust

    async def _open_remote_child_transport(
        self,
        url: str,
        explicit_transport: str,
        headers: dict[str, str] | None,
        oauth_bearer_headers: dict[str, str] | None,
        trust: _typing.Any,
        allowed_private_hosts: list[str],
        stack: contextlib.AsyncExitStack,
    ) -> tuple[_typing.Any, _typing.Any]:
        from arbora.core.transport import create_async_http_client

        def _secure_httpx_factory(
            headers: dict[str, str] | None = None,
            timeout: _typing.Any = None,
            auth: _typing.Any = None,
        ):
            return create_async_http_client(
                timeout=timeout or 30.0,
                verify=trust.ssl_context,
                headers=headers,
                auth=auth,
                trust_env=False,
                follow_redirects=False,
                pin_egress=True,
                allowed_private_hosts=allowed_private_hosts,
            )

        # A0 (CONCEPT:MERISTEM-OS.identity.so-jwt-protected-children): authenticate jwt-protected children with the
        # multiplexer's service-account bearer. Opt-in via
        # MCP_CLIENT_AUTH=oidc-client-credentials; never overrides a child's
        # own Authorization header; a mint failure aborts the connection.
        # Use a per-request httpx.Auth (not a frozen header): the child's
        # pooled session is long-lived, so a baked-in short-lived token would
        # expire mid-session and wedge calls on a 401 (CONCEPT:MERISTEM-OS.identity.so-jwt-protected-children).
        #
        # NE-008: an oauth-gated child's authorization model IS the
        # caller's own per-principal delegated grant (just placed into
        # ``headers`` above) -- Arbora's own service-to-child credential
        # is deliberately NOT also computed for it, so the two credential
        # lifetimes (service <-> child, and user <-> provider) are never
        # merged onto the same outbound request.
        if oauth_bearer_headers is not None:
            _svc_auth = None
        else:
            from arbora.security.client_credentials import child_auth

            _svc_auth = child_auth(headers, self._fleet.config)
        use_sse = explicit_transport == "sse" or url.rstrip("/").endswith("/sse")
        if use_sse:
            # D-MTT-1: `_svc_auth` is a local `httpx.Auth` (see
            # `child_auth`'s docstring); `sse_client`'s `auth` param is
            # typed `httpx2.Auth | None` (fastmcp's vendored SDK v2 HTTP
            # client, a distinct package from this repo's own `httpx` —
            # see `arbora/core/httpx_boundary/py`). Coerce at
            # this boundary rather than passing the foreign-typed object
            # straight through.
            from arbora.core.httpx_boundary import coerce_httpx2_auth

            transport = sse_client(
                url,
                headers=headers,
                auth=coerce_httpx2_auth(_svc_auth),
                httpx_client_factory=_secure_httpx_factory,
            )
        else:
            # MCP SDK v2 takes the already-built client instead of
            # headers/auth/httpx_client_factory, so the security-hardened
            # client (pinned TLS trust, DNS-pinned egress, no ambient
            # proxy, no redirects) is constructed here and handed over.
            # It is entered on the stack BEFORE the transport so teardown
            # closes the transport first and the client second; the SDK
            # deliberately does not close a caller-provided client.
            http_client = _secure_httpx_factory(headers=headers, auth=_svc_auth)
            await stack.enter_async_context(http_client)
            transport = streamable_http_client(
                url,
                http_client=http_client,
                max_sse_event_size=_MAX_CHILD_SSE_EVENT_BYTES,
            )
        # streamable-http and sse both yield (read, write); SDK v2 dropped
        # streamable-http's third `get_session_id` element. Take the first
        # two streams either way.
        streams = await stack.enter_async_context(transport)
        return streams[0], streams[1]

    async def _open_remote_child_session_streams(
        self,
        cfg: dict,
        url: str,
        explicit_transport: str,
        stack: contextlib.AsyncExitStack,
    ) -> tuple[_typing.Any, _typing.Any]:
        parsed_url = self._validate_remote_child_url(url)
        allowed_private_hosts = self._resolve_remote_allowed_private_hosts(
            cfg, parsed_url, url, self._fleet.config
        )
        headers = self._validate_remote_child_headers(cfg, self._fleet.config)
        headers, oauth_bearer_headers = await self._apply_remote_child_oauth_grant(
            cfg, url, headers
        )
        trust = self._resolve_remote_child_tls_trust(cfg, stack, self._fleet.config)
        return await self._open_remote_child_transport(
            url,
            explicit_transport,
            headers,
            oauth_bearer_headers,
            trust,
            allowed_private_hosts,
            stack,
        )

    @staticmethod
    def _local_child_process_config_valid(
        command: str, args: list, configured_env: dict
    ) -> bool:
        return not (
            not 1 <= len(command) <= 4_096
            or "\x00" in command
            or not isinstance(args, list)
            or len(args) > 128
            or not all(
                isinstance(value, str) and len(value) <= 8_192 and "\x00" not in value
                for value in args
            )
            or not isinstance(configured_env, dict)
            or len(configured_env) > 256
        )

    @staticmethod
    def _validate_local_child_command_and_args(
        command: str, cfg: dict, config: RuntimeConfig
    ) -> tuple[str, list, dict]:
        # `command` is guaranteed set here: the earlier guard raises unless
        # `command` or `is_remote` is truthy, and this is the `not is_remote`
        # branch.
        assert command, "unreachable: non-remote server must have a 'command'"
        command = resolve_runtime_value(command, sensitive=False, config=config)
        raw_args = cfg.get("args", [])
        args = (
            [
                resolve_runtime_value(value, sensitive=False, config=config)
                for value in raw_args
            ]
            if isinstance(raw_args, list)
            else raw_args
        )
        configured_env = cfg.get("env") or {}
        if not ChildTransport._local_child_process_config_valid(
            command, args, configured_env
        ):
            raise RuntimeError("Local MCP child process configuration is invalid")
        return command, args, configured_env

    @staticmethod
    def _apply_one_configured_env_var(
        merged_env: dict[str, str],
        raw_key: _typing.Any,
        raw_value: _typing.Any,
        provider_controlled_keys: set[str],
        materialized: set[str],
        config: RuntimeConfig,
    ) -> None:
        key = str(raw_key)
        if key.upper() in (
            _PROVIDER_CHILD_ENV_KEYS
            | provider_controlled_keys
            | {_TASK_DELEGATION_CHANNEL_ENV}
        ):
            raise RuntimeError("MCP child provider environment is parent-controlled")
        value = resolve_runtime_value(
            raw_value,
            sensitive=_sensitive_config_key(key),
            materialized=key in materialized,
            config=config,
        )
        if (
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", key)
            or len(value) > 65_536
            or "\x00" in value
        ):
            raise RuntimeError("Local MCP child environment is invalid")
        merged_env[key] = value

    @staticmethod
    def _apply_generation_secret(
        merged_env: dict[str, str], generation_secret: str | None
    ) -> None:
        if generation_secret is not None:
            if not 32 <= len(generation_secret) <= 512:
                raise RuntimeError("Local MCP task channel secret is invalid")
            merged_env[_TASK_DELEGATION_CHANNEL_ENV] = generation_secret

    @staticmethod
    def _build_local_child_environment(
        cfg: dict,
        provider_environment: dict[str, str],
        configured_env: dict,
        generation_secret: str | None,
        config: RuntimeConfig,
    ) -> dict[str, str]:
        # A child receives only execution/runtime trust variables plus the
        # variables explicitly delegated in its own catalog entry. Copying
        # the entire parent environment leaks unrelated fleet credentials.
        provider_controlled_keys = {key.upper() for key in provider_environment}
        merged_env = {
            key: value
            for key, value in inherited_environment().items()
            if key.upper() in _CHILD_ENV_ALLOWLIST
            and key.upper() not in provider_controlled_keys
        }
        merged_env.update(provider_environment)
        materialized = (
            set(cfg.get("_runtime_materialized_secret_keys") or [])
            if _runtime_materialized(cfg)
            else set()
        )
        for raw_key, raw_value in configured_env.items():
            ChildTransport._apply_one_configured_env_var(
                merged_env,
                raw_key,
                raw_value,
                provider_controlled_keys,
                materialized,
                config,
            )
        ChildTransport._apply_generation_secret(merged_env, generation_secret)
        return merged_env

    @staticmethod
    def _verify_child_runtime_policy_before_spawn(runtime_policy: _typing.Any) -> None:
        if runtime_policy is not None:
            try:
                runtime_policy.verify_before_spawn()
            except Exception:
                logger.warning(
                    "_verify_child_runtime_policy_before_spawn failed", exc_info=True
                )
                raise RuntimeError(
                    "MCP child runtime policy pre-spawn verification failed"
                ) from None

    @staticmethod
    async def _open_stdio_child_transport(
        server_params: _mcp.StdioServerParameters, stack: contextlib.AsyncExitStack
    ) -> tuple[_typing.Any, _typing.Any]:
        # The MCP SDK otherwise forwards a child's raw stderr to the parent
        # process. Import and native-loader failures routinely contain
        # interpreter, checkout, and trust-material locations. The parent
        # already emits bounded transport/error codes, so discard that raw
        # channel and keep the sink alive for the complete child generation.
        child_error_sink = stack.enter_context(
            Path(os.devnull).open("w", encoding="utf-8")
        )
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(server_params, errlog=child_error_sink)
        )
        return read_stream, write_stream

    async def _open_local_child_session_streams(
        self,
        cfg: dict,
        command: str,
        provider_environment: dict[str, str],
        runtime_policy: _typing.Any,
        generation_secret: str | None,
        stack: contextlib.AsyncExitStack,
    ) -> tuple[_typing.Any, _typing.Any]:
        command, args, configured_env = self._validate_local_child_command_and_args(
            command, cfg, self._fleet.config
        )
        merged_env = self._build_local_child_environment(
            cfg,
            provider_environment,
            configured_env,
            generation_secret,
            self._fleet.config,
        )
        server_params = StdioServerParameters(
            command=command, args=args, env=merged_env
        )
        self._verify_child_runtime_policy_before_spawn(runtime_policy)
        return await self._open_stdio_child_transport(server_params, stack)

    def set_handshake_listener(
        self, listener: _typing.Callable[..., None] | None
    ) -> None:
        """Install the callback that schedules onboarding after a handshake."""
        self._handshake_listener = listener

    async def _announce_handshake(
        self,
        server_name: str,
        session: _typing.Any,
        tools: list[_typing.Any],
        reason: str = "handshake",
    ) -> None:
        """Tell the listener a child handshake completed; never raises.

        Runs after ``initialize`` or ``server/discover`` and ``tools/list``.
        The change signal is a content digest of the served tool, prompt and
        resource lists and of ``connector://resources``, never the serverInfo
        version (ARBORA-FLEET-R041.8). The listener only schedules work off
        this path, so serving is never delayed or failed by onboarding
        (ARBORA-FLEET-R041.7, R041.9).
        """
        listener = self._handshake_listener
        if listener is None:
            return
        try:
            from arbora.fleet.onboarding import served_catalog_digest

            # ``server_info`` is set by both an initialize and a discover
            # session; ``initialize_result`` is empty on a discover session.
            info = getattr(session, "server_info", None)
            listener(
                server_name,
                server_version=str(getattr(info, "version", "") or ""),
                digest=await served_catalog_digest(session, tools),
                reason=reason,
            )
        except Exception as exc:
            logger.warning(
                "handshake onboarding not scheduled: %s",
                redact_for_log(exc),
                exc_info=True,
            )

    def _child_message_handler(
        self, server_name: str, holder: list[_typing.Any]
    ) -> MessageHandlerFnT:
        """Re-capture a child whose lists change (ARBORA-FLEET-R041.8).

        A server that advertises ``listChanged`` sends tool, prompt or resource
        list-changed notifications; each one re-digests the child and asks the
        one onboarding pass to run again. A server that sends none is covered
        by the next handshake.
        """
        changed = (
            mcp_types.ToolListChangedNotification,
            mcp_types.PromptListChangedNotification,
            mcp_types.ResourceListChangedNotification,
        )

        async def refresh() -> None:
            session = holder[0] if holder else None
            if session is None:
                return
            try:
                tools = await _list_all_tools(session)
                _bounded_tool_catalog(tools)
            except Exception as exc:
                logger.warning(
                    "list_changed refresh of %s failed: %s",
                    server_name,
                    redact_for_log(exc),
                    exc_info=True,
                )
                return
            # ARBORA-FLEET-R070: the new list replaces the served one now,
            # without a restart; the next catalog read re-probes the child.
            self._probe_cache.pop(server_name, None)
            await self._publish_live_tools(server_name, tools)
            await self._announce_handshake(server_name, session, tools, "list_changed")

        async def handler(message: IncomingMessage) -> None:
            if isinstance(message, changed):
                task = asyncio.ensure_future(refresh())
                self._list_changed_tasks.add(task)
                task.add_done_callback(self._list_changed_tasks.discard)

        return handler

    async def _open_one_session(
        self,
        server_name: str,
        cfg: dict,
        stack: contextlib.AsyncExitStack,
        generation_secret: str | None = None,
    ) -> ClientSession:
        """Open + initialize ONE ``ClientSession`` for a child (stdio or remote),
        entering its transports on ``stack``. Raises on failure. Shared by
        :meth:`_start_child` (session pool) and :meth:`probe_server` (catalog
        probe) so the transport-construction logic lives in one place."""
        command, url, explicit_transport, is_remote = (
            self._resolve_transport_kind_for_child(server_name, cfg)
        )
        initialization_timeout = self._resolve_child_initialization_timeout(cfg)

        (
            provider_environment,
            runtime_policy,
        ) = await self._resolve_child_provider_environment(
            cfg, is_remote, stack, initialization_timeout
        )

        if is_remote:
            read_stream, write_stream = await self._open_remote_child_session_streams(
                cfg, url, explicit_transport, stack
            )
        else:
            read_stream, write_stream = await self._open_local_child_session_streams(
                cfg,
                command,
                provider_environment,
                runtime_policy,
                generation_secret,
                stack,
            )

        holder: list[_typing.Any] = []
        session = await stack.enter_async_context(
            ClientSession(
                read_stream,
                write_stream,
                message_handler=self._child_message_handler(server_name, holder),
                # ARBORA-FLEET-R015.2: confirm only a plan-confirmed armed call.
                elicitation_callback=self._fleet.confirmations.answer_elicitation,
            )
        )
        holder.append(session)
        # Cold starts can legitimately exceed 30 seconds on constrained hosts
        # (for example, a Python MCP child importing from a WSL-mounted volume).
        # Keep the handshake bounded, but honor the same per-child connect
        # budget rather than imposing a second, hidden fixed ceiling.
        await asyncio.wait_for(session.initialize(), timeout=initialization_timeout)
        # Production capability state is read directly from the live
        # _child_resilience.ChildRuntime session pool. A retired runtime can finish a late
        # reconnect after a catalog epoch changes, so this transport-open path
        # mutates no shared handshake record.
        return session
