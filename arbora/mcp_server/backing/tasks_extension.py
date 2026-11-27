"""Native Arbora ``io.modelcontextprotocol/tasks`` extension, backed by WorkItem.

CONCEPT:MERISTEM-ECO.mcp.tasks-workitem-bridge

Serves the WorkItem-backed Tasks contract for a client connected directly to
Arbora's own FastMCP 4 server, over whichever MCP protocol version that
client negotiates -- ``mcp>=2.0.0`` is now the default (see
``server_factory.py``, ``docs/architecture/fastmcp4-default.md``), and the
2026-07-28 stateless single-exchange transport it enables is served natively
by the installed SDK on this same server, not a separate process (BUG-069;
see ``docs/architecture/mcp-2026-protocol-surface.md``). This module used to
mirror an equivalent projection an isolated ``mcp_v2_gateway`` sidecar
carried over its own HTTP hop to a legacy Arbora session; that sidecar has
been retired -- this is now the only implementation.

Deliberately does **not** mount ``fastmcp_tasks.extension.TasksExtension``:
that package's execution engine is hard-wired to a Docket/Redis backend
(``fastmcp_tasks.handlers``/``creation.py``, ``pydocket`` dependency) -- a
second, parallel job system this codebase's one ``WorkItem`` state machine
(``MERISTEM-P1-1``) already forbids duplicating (``AGENTS.md``: extend before you
add). This module only reuses the extension's *registration* mechanism
(``fastmcp.server.extensions.ServerExtension``/``MethodBinding``) -- never
its execution engine -- and backs every method with
``meristem.knowledge_graph.core.work_durability`` directly, the exact same
authority ``graph_jobs`` (``arbora/mcp_server/backing/tools/job_tools/py``) uses.
The wire model field names below follow ``fastmcp_tasks.models`` (the
current SEP-2663 draft: flat task fields, ``inputRequests`` for
``input_required``) rather than the older, reduced revision the retired
sidecar had pinned to (its own ``TASKS_EXTENSION_REVISION``) for reasons
that no longer apply once there is only one implementation.
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from arbora import work_items
from arbora.core.environment import read_env
from arbora.core.settings import RuntimeConfig
from arbora.fleet.protocol_compat import mcp_protocol_exception
from arbora.fleet.tasks_wire import (
    TASKS_EXTENSION_ID,
    TASKS_EXTENSION_REVISION,
    AckResult,
    CancelTaskParams,
    GetTaskParams,
    GetTaskResult,
    UpdateTaskParams,
    bound_signer,
    channel_proof,
    delegation_binding,
)
from arbora.mcp_server.served_engine import engine_unbound
from arbora.security.run_token import RunTokenSigner

# MCP SDK v2 (which the `fastmcp>=4.0.0b1` floor pulls in) renamed
# `McpError` -> `MCPError`. The fleet is EXPLICITLY mixed-version: child images
# still ship fastmcp 3.x / SDK v1 while hostPath-mounting THIS working tree over
# their own site-packages, so a hard `from mcp.shared.exceptions import MCPError`
# raises ImportError at MODULE scope on every one of them (D-W2C2-3). That is the
# same class of break as the `fastmcp.server.extensions` guard below, one SDK
# down. `mcp_protocol_error()` binds whichever spelling the installed SDK
# exposes and raises loudly if neither does — it never degrades to a benign
# default, because a silently-unbound error type made `is_session_dead` return
# False for every exception once already. Errors this extension emits are built
# through the same resolver, preserving the installed SDK's wire exception.

# CONCEPT:MERISTEM-ECO.mcp.tasks-workitem-bridge -- the fleet is EXPLICITLY mixed
# fastmcp-version (D-SH-3: child images still ship fastmcp 3.4.4 while the
# canonical working tree, hostPath-mounted directly over each pod's
# site-packages, targets fastmcp>=4.0.0b1 -- see docs/architecture/
# fastmcp4-default.md). `fastmcp.server.extensions` (ServerExtension,
# MethodBinding, read_client_extension_settings) is a fastmcp-4-only module;
# importing it unguarded at module scope takes the WHOLE server down before
# anything else runs (D-W2C2-2: 58 fleet pods in CrashLoopBackOff on exactly
# this `ModuleNotFoundError`). Mirrors the same style already used for the
# MCP-SDK v1/v2 `McpError`/`MCPError` rename in `protocol_compat.py` and for
# optional middlewares in `server_factory._configure_middleware`: guard the
# import, degrade to a no-op capability, and log loudly so an operator can
# tell "Tasks extension unavailable on this image" from "server broken".
#
# ★ The guard covers `mcp_types` too. `mcp_types` is a fastmcp-4/SDK-v2-only
# distribution, so on a fastmcp-3 image `from mcp_types import ...` fails at
# module scope for exactly the same reason and with exactly the same blast
# radius. Guarding only `fastmcp.server.extensions` moved the crash three lines
# up rather than fixing it (observed live: 58 pods cleared the extensions
# import and then died on `mcp_types`). ONE guard, ONE failure mode, ONE flag —
# every fastmcp-4-only symbol this module needs is bound here or not at all.
if TYPE_CHECKING:
    from fastmcp.server.extensions import (
        MethodBinding,
        ServerExtension,
        read_client_extension_settings,
    )
    from mcp_types.jsonrpc import MISSING_REQUIRED_CLIENT_CAPABILITY
    from mcp_types.version import MODERN_PROTOCOL_VERSIONS

    TASKS_EXTENSION_AVAILABLE = True
    _TASKS_EXTENSION_IMPORT_ERROR: ImportError | None = None
else:
    try:
        from fastmcp.server.extensions import (
            MethodBinding,
            ServerExtension,
            read_client_extension_settings,
        )
        from mcp_types.jsonrpc import MISSING_REQUIRED_CLIENT_CAPABILITY
        from mcp_types.version import MODERN_PROTOCOL_VERSIONS
    except ImportError as _fastmcp_extensions_import_error:
        TASKS_EXTENSION_AVAILABLE = False
        _TASKS_EXTENSION_IMPORT_ERROR: ImportError | None = (
            _fastmcp_extensions_import_error
        )

        # -32002 is the SDK's own code for this condition; the value is inert here
        # because every path that reads it is gated behind TASKS_EXTENSION_AVAILABLE.
        MISSING_REQUIRED_CLIENT_CAPABILITY = -32002
        # Empty, so `_TASK_METHOD_VERSIONS` matches no protocol version at all —
        # the Tasks methods are correctly invisible on an image that cannot serve them.
        MODERN_PROTOCOL_VERSIONS: tuple[str, ...] = ()

        # Fallback stand-ins so `class WorkItemTasksExtension(ServerExtension)`
        # below still defines cleanly (it overrides `methods()` itself, so the
        # real base class's behavior is never needed on this path). Neither is
        # ever exercised for real: `server_factory.create_mcp_server` checks
        # `TASKS_EXTENSION_AVAILABLE` before calling `mcp.add_extension(...)`, so
        # `methods()` -- the only place `MethodBinding`/
        # `read_client_extension_settings` are used -- is never invoked. If some
        # other caller ever does instantiate the extension directly on a
        # fastmcp-3 image and reach that path, this raises the ORIGINAL
        # ModuleNotFoundError (chained, not swallowed) instead of a confusing
        # `NameError`.
        class ServerExtension:
            """Stand-in for ``fastmcp.server.extensions.ServerExtension`` (fastmcp<4)."""

            __slots__ = ()

        class MethodBinding:
            def __init__(self, *_args: Any, **_kwargs: Any) -> None:
                raise ModuleNotFoundError(
                    "fastmcp.server.extensions.MethodBinding requires fastmcp>=4.0.0b1"
                ) from _TASKS_EXTENSION_IMPORT_ERROR

        def read_client_extension_settings(*_args: Any, **_kwargs: Any) -> Any:
            raise ModuleNotFoundError(
                "fastmcp.server.extensions.read_client_extension_settings requires "
                "fastmcp>=4.0.0b1"
            ) from _TASKS_EXTENSION_IMPORT_ERROR
    else:
        TASKS_EXTENSION_AVAILABLE = True
        _TASKS_EXTENSION_IMPORT_ERROR = None

if TYPE_CHECKING:
    from mcp.server.context import ServerRequestContext

logger = logging.getLogger(__name__)

_TASK_DELEGATION_CHANNEL_ENV = "ARBORA_MCP_TASK_CHANNEL_SECRET"
_TASK_METHOD_VERSIONS = frozenset(MODERN_PROTOCOL_VERSIONS)


def _installed_fastmcp_version() -> str:
    """Best-effort ``fastmcp`` version string for the degrade-mode warning."""
    try:
        import importlib.metadata

        return f"fastmcp=={importlib.metadata.version('fastmcp')}"
    except (
        importlib.metadata.PackageNotFoundError
    ):  # purely cosmetic, never block the warning itself
        return "an unknown fastmcp version"


if not TASKS_EXTENSION_AVAILABLE:
    # Preserve and surface the cause (never a bare/silent fallback -- swallowed
    # ImportErrors have twice destroyed the diagnosis path on this program
    # today). `from None` is deliberately NOT used below: the original
    # ModuleNotFoundError is chained via `raise ... from` at the one call site
    # that actually needs to fail (`WorkItemTasksExtension()` is never
    # constructed on this path), and is logged here so it shows up even when
    # nothing ever tries to instantiate the extension.
    logger.warning(
        "tasks_extension: WorkItem Tasks extension (%s) unavailable on this "
        "image -- requires fastmcp>=4.0.0b1 (fastmcp.server.extensions), this "
        "server has %s. The server will start WITHOUT tasks/get, "
        "tasks/update, tasks/cancel; every other capability is unaffected. "
        "Root cause: %s",
        TASKS_EXTENSION_ID,
        _installed_fastmcp_version(),
        _TASKS_EXTENSION_IMPORT_ERROR,
    )

# WorkItem raw statuses that project onto the extension's "working" (or, with
# a live pending_input_request, "input_required") wire status. Mirrors
# mcp_v2_gateway.gateway.ArboraV2Gateway._WORKING_RAW_STATUSES, but this
# module targets `graph_jobs`'s orchestrator-dispatch WorkItems, whose raw
# vocabulary never includes "queued"/"pending"/"executing" (those are
# ingest/legacy-bridge synonyms projected only by the gateway's downstream
# `graph_jobs(action="status")` JSON, not the raw WORK_ITEM_STATES tuple).
_WORKING_RAW_STATUSES = frozenset({"submitted", "ready", "leased", "running"})


def _orchestrator_work_item_id(task_id: str) -> str:
    """The deterministic WorkItem id of one queued orchestrator task."""
    return f"workitem:orchestrator:{task_id}"


def _iso_timestamp(value: Any) -> str:
    if isinstance(value, bool):
        raise mcp_protocol_exception(-32603, "Task timestamp was invalid")
    if isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise mcp_protocol_exception(-32603, "Task timestamp was invalid") from None
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if isinstance(value, datetime):
        parsed = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        return (
            parsed.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise mcp_protocol_exception(-32603, "Task timestamp was invalid") from None
        parsed = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
        return (
            parsed.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
    raise mcp_protocol_exception(-32603, "Task timestamp was invalid")


class WorkItemTasksExtension(ServerExtension):
    """Backs ``tasks/get``/``tasks/update``/``tasks/cancel`` with native WorkItems.

    The native WorkItem state machine remains the only task authority.  Both
    graph orchestrator IDs and Repository Manager's durable ``rmjob`` /
    ``workitem:repository_manager`` IDs are projections of that one state,
    never a second queue.  Repository IDs are resolved through the
    tenant/owner-scoped adapter so a task poll, input update, or cancellation
    remains safe after a process restart or on a replica.

    ``server_id`` is included in response metadata and is used by the arbora
    multiplexer to route follow-up task requests to the owning server.  The
    optional ``task_router`` is a narrow request forwarder; it does not own
    persistence, lifecycle, or execution.
    """

    identifier = TASKS_EXTENSION_ID

    def __init__(
        self,
        *,
        server_id: str | None = None,
        task_router: Any | None = None,
        engine_provider: Callable[[], Any] = engine_unbound,
        signer: RunTokenSigner | None = None,
        config: RuntimeConfig,
    ) -> None:
        self._config = config
        self._engine_provider = engine_provider
        self._signer = signer
        self.server_id = (
            str(server_id).strip()
            if isinstance(server_id, str) and server_id.strip()
            else None
        )
        self._task_router = task_router

    def settings(self) -> dict[str, Any]:
        """Advertise the exact projection revision for capability routing."""

        return {"revision": TASKS_EXTENSION_REVISION}

    def set_task_router(self, router: Any | None) -> None:
        """Attach the owning arbora multiplexer request router.

        FastMCP keeps extension instances on the host server.  The
        multiplexer is attached later, after the fleet catalog is loaded, so
        this setter avoids registering a second Tasks extension or task
        backend.  A router is deliberately duck-typed to keep this module
        usable by direct servers and lightweight tests.
        """

        self._task_router = router

    def methods(self) -> list[MethodBinding]:
        return [
            MethodBinding(
                method="tasks/get",
                params_type=GetTaskParams,
                handler=self._handle_get,
                protocol_versions=_TASK_METHOD_VERSIONS,
            ),
            MethodBinding(
                method="tasks/update",
                params_type=UpdateTaskParams,
                handler=self._handle_update,
                protocol_versions=_TASK_METHOD_VERSIONS,
            ),
            MethodBinding(
                method="tasks/cancel",
                params_type=CancelTaskParams,
                handler=self._handle_cancel,
                protocol_versions=_TASK_METHOD_VERSIONS,
            ),
        ]

    def _require_tasks_capability(self, ctx: ServerRequestContext[Any, Any]) -> None:
        """SEP-2663: reject a task method the client did not opt into this request."""
        if read_client_extension_settings(ctx, TASKS_EXTENSION_ID) is None:
            raise mcp_protocol_exception(
                MISSING_REQUIRED_CLIENT_CAPABILITY,
                (
                    f"This request targets the tasks extension "
                    f"({TASKS_EXTENSION_ID}); the client did not declare it "
                    "for this request."
                ),
                {"requiredCapabilities": {"extensions": {TASKS_EXTENSION_ID: {}}}},
            )

    @staticmethod
    def _actor_id(session: Any) -> str:
        actor = getattr(session, "actor", None)
        actor_id = str(getattr(actor, "actor_id", "") or "").strip()
        if not actor_id:
            raise mcp_protocol_exception(-32001, "Verified task owner is unavailable")
        return actor_id

    @classmethod
    def _caller_metadata(
        cls, session: Any, *, required_scope: str | None = None
    ) -> dict[str, Any]:
        """Return bounded identity metadata for a multiplexer hop.

        The values originate from the verified GraphSession, never request
        fields.  A child server still performs its own local authorization;
        this envelope is for route/audit continuity across a trusted
        multiplexer connection.
        """

        scopes = (
            (required_scope,)
            if required_scope is not None
            else getattr(session, "scopes", ())
        )
        return {
            "tenant": str(getattr(session, "tenant", "") or ""),
            "owner": cls._actor_id(session),
            "scopes": sorted(str(scope) for scope in scopes),
        }

    @staticmethod
    def _validate_route_server(server: Any) -> str:
        if (
            not isinstance(server, str)
            or not server.strip()
            or any(ord(char) < 0x20 for char in server)
        ):
            raise mcp_protocol_exception(-32602, "Invalid Tasks owning-server route")
        return server.strip()

    @staticmethod
    def _validate_route_revision(revision: Any) -> None:
        if revision != TASKS_EXTENSION_REVISION:
            raise mcp_protocol_exception(
                -32602,
                "Unsupported Tasks extension revision",
                {"expectedRevision": TASKS_EXTENSION_REVISION},
            )

    @staticmethod
    def _validate_delegation_token(token: Any, issuer: Any) -> str:
        if (
            issuer != "mcp-multiplexer"
            or not isinstance(token, str)
            or not 32 <= len(token) <= 16_384
            or any(ord(character) < 0x20 for character in token)
        ):
            raise mcp_protocol_exception(-32001, "Invalid delegated task proof")
        return token

    @staticmethod
    def _validate_delegation_channel(channel: Any) -> str:
        if (
            not isinstance(channel, str)
            or len(channel) != 64
            or any(character not in "0123456789abcdef" for character in channel)
        ):
            raise mcp_protocol_exception(-32001, "Invalid delegated task proof")
        return channel

    @staticmethod
    def _build_route_delegation(delegation: Any) -> dict[str, Any]:
        if not isinstance(delegation, Mapping):
            raise mcp_protocol_exception(-32001, "Invalid delegated task proof")
        token = WorkItemTasksExtension._validate_delegation_token(
            delegation.get("token"), delegation.get("issuer")
        )
        result: dict[str, Any] = {"issuer": "mcp-multiplexer", "token": token}
        channel = delegation.get("channel")
        if channel is not None:
            result["channel"] = WorkItemTasksExtension._validate_delegation_channel(
                channel
            )
        return result

    @staticmethod
    def _route_from_params(params: Any) -> dict[str, Any] | None:
        meta = getattr(params, "meta", None)
        if not isinstance(meta, Mapping):
            return None
        raw = meta.get(TASKS_EXTENSION_ID)
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise mcp_protocol_exception(-32602, "Invalid Tasks route metadata")
        server = WorkItemTasksExtension._validate_route_server(raw.get("server"))
        revision = raw.get("revision")
        WorkItemTasksExtension._validate_route_revision(revision)
        route: dict[str, Any] = {"server": server, "revision": revision}
        # Preserve only the route target/revision.  Caller identity is
        # accepted below only as a delegated envelope emitted by the
        # multiplexer; a direct client cannot forge echoed authority metadata.
        caller = raw.get("caller")
        if isinstance(caller, Mapping):
            route["caller"] = dict(caller)
        delegation = raw.get("delegation")
        if delegation is not None:
            route["delegation"] = WorkItemTasksExtension._build_route_delegation(
                delegation
            )
        return route

    @staticmethod
    def _authorized_session(ctx: ServerRequestContext[Any, Any], scope: str) -> Any:
        """Resolve verified identity for extension requests (including HTTP).

        ``ActorContextMiddleware`` historically scoped tool calls only.  A
        native extension request is not a ``tools/call``, so use the ambient
        session when present and otherwise mint the same immutable authority
        from FastMCP's already-validated access token.  No token means fail
        closed; this never accepts tenant/owner from task params.
        """

        from arbora.security.session import (
            SessionRequiredError,
            resolve_session,
        )

        try:
            return resolve_session(required_scope=scope)
        except SessionRequiredError:  # falls through to mint from the access token
            pass

        try:
            from fastmcp.server.dependencies import get_access_token

            token = get_access_token()
            claims = getattr(token, "claims", None) if token is not None else None
            if not isinstance(claims, Mapping) or not claims:
                raise PermissionError("Verified graph authority is required")
            from arbora.security.request_identity import (
                actor_from_claims,
                mint_graph_session,
            )

            session = mint_graph_session(actor_from_claims(dict(claims)))
            session.require_scope(scope)
            return session
        except PermissionError:
            raise
        except Exception as exc:  # convert auth failures to a stable protocol error
            logger.warning(
                "tasks_extension: failed to resolve verified request identity (%s)",
                type(exc).__name__,
                exc_info=True,
            )
            raise PermissionError("Verified graph authority is required") from None

    @staticmethod
    def _stdio_delegation_authority() -> Any | None:
        """Local stdio children have no HTTP bearer context. Their parent
        injects a random, per-connection-generation channel secret into the
        child process environment; the request proof below must present the
        corresponding MAC. This secret is never accepted from catalog JSON.
        Returns ``None`` when no (valid-length) stdio channel secret is set."""

        channel_secret = read_env(_TASK_DELEGATION_CHANNEL_ENV, "").strip()
        if not 32 <= len(channel_secret) <= 512:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(
            authenticated=True,
            issuer="stdio:mcp-multiplexer",
            audience="stdio-child-generation",
            service_principal="mcp-multiplexer",
            scopes=frozenset({"mcp:delegate"}),
            channel_secret=channel_secret,
        )

    @staticmethod
    def _delegator_token_and_claims() -> tuple[Any, Mapping[str, Any]]:
        try:
            from fastmcp.server.dependencies import get_access_token

            token = get_access_token()
        except (ImportError, RuntimeError, LookupError):
            token = None
        claims = getattr(token, "claims", None) if token is not None else None
        if not isinstance(claims, Mapping) or not claims:
            raise mcp_protocol_exception(
                -32001,
                "Authenticated multiplexer service authority is required",
            )
        return token, claims

    @staticmethod
    def _configured_issuers(config: RuntimeConfig) -> set[str]:
        expected_issuer = (
            getattr(config, "auth_jwt_issuer", None)
            or getattr(config, "mcp_jwt_issuer", None)
            or read_env("FASTMCP_SERVER_AUTH_JWT_ISSUER")
            or read_env("AUTH_JWT_ISSUER")
            or read_env("MCP_JWT_ISSUER")
        )
        return {
            value.strip()
            for value in str(expected_issuer or "").split(",")
            if value.strip()
        }

    @staticmethod
    def _configured_audiences(config: RuntimeConfig) -> set[str]:
        expected_audience = (
            getattr(config, "auth_jwt_audience", None)
            or getattr(config, "mcp_jwt_audience", None)
            or read_env("FASTMCP_SERVER_AUTH_JWT_AUDIENCE")
            or read_env("AUTH_JWT_AUDIENCE")
            or read_env("MCP_JWT_AUDIENCE")
        )
        return {
            value.strip()
            for value in str(expected_audience or "").split(",")
            if value.strip()
        }

    @staticmethod
    def _token_audiences(raw_audience: Any) -> set[str]:
        if isinstance(raw_audience, str):
            return {raw_audience.strip()} if raw_audience.strip() else set()
        if isinstance(raw_audience, (list, tuple, set, frozenset)):
            return {str(value).strip() for value in raw_audience if str(value).strip()}
        return set()

    @staticmethod
    def _delegator_capabilities(token: Any, identity: Any, config: Any) -> set[str]:
        from arbora.security.identity import base_capabilities

        capabilities = {
            str(scope).strip()
            for scope in (getattr(token, "scopes", None) or ())
            if str(scope).strip()
        }
        capabilities.update(
            str(scope).strip()
            for scope in base_capabilities(
                identity,
                getattr(config, "identity_group_capability_map", None),
            )
            if str(scope).strip()
        )
        return capabilities

    @staticmethod
    def _delegator_identity_facts(
        token: Any, claims: Mapping[str, Any], config: RuntimeConfig
    ) -> tuple[str, set[str], set[str], set[str], str, set[str]]:
        """(issuer, issuers, token_audiences, audiences, principal,
        capabilities) resolved from ``claims``/``token`` — raises the SAME
        stable protocol error as every other failure mode on this path if
        resolution itself fails."""
        try:
            from arbora.security.identity import normalize_identity

            identity = normalize_identity(claims)
            issuers = WorkItemTasksExtension._configured_issuers(config)
            audiences = WorkItemTasksExtension._configured_audiences(config)
            issuer = str(claims.get("iss") or "").strip()
            token_audiences = WorkItemTasksExtension._token_audiences(claims.get("aud"))
            principal = str(
                getattr(token, "client_id", None)
                or claims.get("client_id")
                or claims.get("azp")
                or ""
            ).strip()
            capabilities = WorkItemTasksExtension._delegator_capabilities(
                token, identity, config
            )
        except Exception:
            logger.warning("delegator authority resolution failed", exc_info=True)
            raise mcp_protocol_exception(
                -32001,
                "Authenticated multiplexer service authority is required",
            ) from None
        return issuer, issuers, token_audiences, audiences, principal, capabilities

    @staticmethod
    def _validate_delegator_facts(
        issuer: str,
        issuers: set[str],
        token_audiences: set[str],
        audiences: set[str],
        principal: str,
        capabilities: set[str],
    ) -> None:
        if (
            not issuers
            or issuer not in issuers
            or not audiences
            or not token_audiences.intersection(audiences)
            or not principal
            or not capabilities.intersection(
                {"mcp:delegate", "mcp:admin", "admin", "kg:admin"}
            )
        ):
            raise mcp_protocol_exception(
                -32001,
                "Authenticated multiplexer service authority is required",
            )

    def _authorized_delegator(self, ctx: ServerRequestContext[Any, Any]) -> Any:
        """Resolve the verified service authority for a delegated task hop.

        The HMAC task proof carries the *end user's* bounded identity, not
        the identity of the MCP connection that delivered it.  A valid HMAC
        alone is therefore insufficient: a direct client that learns the
        shared secret must not be able to impersonate the multiplexer.  The
        inbound MCP bearer is verified by FastMCP before this method sees it;
        this method additionally binds issuer, audience, and an automated
        service principal with the explicit fleet-delegation capability.
        """
        stdio_authority = WorkItemTasksExtension._stdio_delegation_authority()
        if stdio_authority is not None:
            return stdio_authority

        token, claims = WorkItemTasksExtension._delegator_token_and_claims()
        (
            issuer,
            issuers,
            token_audiences,
            audiences,
            principal,
            capabilities,
        ) = WorkItemTasksExtension._delegator_identity_facts(
            token, claims, self._config
        )
        WorkItemTasksExtension._validate_delegator_facts(
            issuer, issuers, token_audiences, audiences, principal, capabilities
        )

        from types import SimpleNamespace

        return SimpleNamespace(
            authenticated=True,
            issuer=issuer,
            audience=next(iter(token_audiences.intersection(audiences))),
            service_principal=principal,
            scopes=frozenset(capabilities),
            channel_secret=None,
        )

    @staticmethod
    def _require_delegator(authority: Any | None) -> None:
        """Defend the private delegated projection against bypass callers."""

        if (
            authority is None
            or not bool(getattr(authority, "authenticated", False))
            or not str(getattr(authority, "issuer", "") or "").strip()
            or not str(getattr(authority, "audience", "") or "").strip()
            or not str(getattr(authority, "service_principal", "") or "").strip()
            or not set(getattr(authority, "scopes", ())).intersection(
                {"mcp:delegate", "mcp:admin", "admin", "kg:admin"}
            )
        ):
            raise mcp_protocol_exception(
                -32001,
                "Authenticated multiplexer service authority is required",
            )

    def _response_meta(
        self, session: Any, route: Mapping[str, Any] | None = None
    ) -> dict[str, Any] | None:
        server = self.server_id
        if not server:
            return None
        payload: dict[str, Any] = {
            "server": server,
            "revision": TASKS_EXTENSION_REVISION,
        }
        # Never echo a caller supplied route envelope.  Responses identify
        # the verified/delegated authority of the server that produced them.
        if session is not None:
            payload["caller"] = self._caller_metadata(session)
        return {TASKS_EXTENSION_ID: payload}

    def _validate_delegated_caller(
        self, route: Mapping[str, Any] | None, session: Any
    ) -> None:
        """Reject a route caller that is not the verified local authority.

        A route envelope is transport metadata, not a credential.  Until the
        multiplexer has a signed delegated-token handoff, the safe contract is
        exact equality with the child request's verified GraphSession.  This
        prevents a direct client from selecting its local server route and
        forging another tenant/owner in response metadata or adapter calls.
        """

        if not isinstance(route, Mapping) or "caller" not in route:
            return
        caller = route.get("caller")
        if not isinstance(caller, Mapping):
            raise mcp_protocol_exception(-32001, "Invalid delegated task identity")
        expected = self._caller_metadata(session)
        from arbora.api.domain.mcp_server.backing.context_helpers import (
            caller_identity_fields,
        )

        normalized = caller_identity_fields(caller)
        if normalized != expected:
            raise mcp_protocol_exception(
                -32001, "Delegated task identity does not match verified authority"
            )

    def _delegated_session(
        self,
        route: Mapping[str, Any] | None,
        *,
        method: str,
        params: Any,
        scope: str,
        service_authority: Any | None = None,
    ) -> Any | None:
        """Verify an authenticated multiplexer-on-behalf-of task envelope.

        The multiplexer mints a short-lived run token using the existing
        ``ARBORA_TOKEN_SECRET`` substrate.  The child validates the
        signature and expiry locally, then checks the token's endpoint,
        operation, tenant, owner, and canonical request binding. A direct
        end-user request cannot forge the delegated tenant/owner metadata.
        """

        if not isinstance(route, Mapping) or "delegation" not in route:
            return None
        self._require_delegator(service_authority)
        caller, proof = self._extract_delegation_caller_and_proof(route)
        token = self._extract_delegation_token(proof)
        self._validate_delegation_channel_proof(service_authority, proof, token)
        decoded = self._decode_delegation_token(token, method)
        self._validatedelegation_binding(decoded, method, params, caller)
        owner, tenant, scopes = self._delegation_identity(caller, decoded)
        self._validate_delegation_scope(scopes, scope)

        from types import SimpleNamespace

        # The delegated session is deliberately narrowed to this request's
        # effective operation.  Carrying the end user's full scope set into
        # the child would make response/audit metadata imply authority that
        # this hop did not need or authorize.
        return SimpleNamespace(
            tenant=tenant,
            scopes=frozenset({scope}),
            actor=SimpleNamespace(actor_id=owner),
        )

    @staticmethod
    def _extract_delegation_caller_and_proof(
        route: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        caller = route.get("caller")
        proof = route.get("delegation")
        if not isinstance(caller, Mapping) or not isinstance(proof, Mapping):
            raise mcp_protocol_exception(-32001, "Invalid delegated task identity")
        return caller, proof

    @staticmethod
    def _extract_delegation_token(proof: Mapping[str, Any]) -> str:
        token = proof.get("token")
        if not isinstance(token, str) or not token:
            raise mcp_protocol_exception(
                -32001,
                "Authenticated task delegation is unavailable; use portable rm_jobs tools",
            )
        return token

    @staticmethod
    def _validate_delegation_channel_proof(
        service_authority: Any, proof: Mapping[str, Any], token: str
    ) -> None:
        channel = proof.get("channel")
        channel_secret = str(
            getattr(service_authority, "channel_secret", "") or ""
        ).strip()
        if channel_secret:
            if not isinstance(channel, str) or not hmac.compare_digest(
                channel, channel_proof(channel_secret, token)
            ):
                raise mcp_protocol_exception(-32001, "Invalid delegated task proof")
        elif channel is not None:
            # A channel MAC is meaningful only when the child has the private
            # per-generation secret; never accept it as an unsigned hint on a
            # remote bearer-authenticated connection.
            raise mcp_protocol_exception(-32001, "Invalid delegated task proof")

    def _decode_delegation_token(self, token: str, method: str) -> Any:
        try:
            return bound_signer(self._signer).validate(
                token,
                endpoint=str(self.server_id or ""),
                operation=method,
            )
        except Exception:
            logger.warning("delegated task proof rejected", exc_info=True)
            raise mcp_protocol_exception(
                -32001, "Invalid or expired delegated task proof"
            ) from None

    def _validatedelegation_binding(
        self, decoded: Any, method: str, params: Any, caller: Mapping[str, Any]
    ) -> None:
        binding = delegation_binding(
            method,
            params.model_dump(mode="json", by_alias=True, exclude_none=True),
            server=str(self.server_id or ""),
            revision=TASKS_EXTENSION_REVISION,
            caller=caller,
        )
        expected = f"mcp-task:{binding}"
        if decoded.run_id != expected:
            raise mcp_protocol_exception(
                -32001, "Delegated task request binding is invalid"
            )

    def _delegation_identity(
        self, caller: Mapping[str, Any], decoded: Any
    ) -> tuple[str, str, frozenset[str]]:
        owner = str(caller.get("owner") or "").strip()
        tenant = str(caller.get("tenant") or "").strip()
        scopes = frozenset(str(value) for value in caller.get("scopes", ()))
        if (
            not owner
            or not tenant
            or decoded.actor_id != owner
            or decoded.tenant_id != tenant
            or decoded.project != self.server_id
        ):
            raise mcp_protocol_exception(
                -32001, "Delegated task identity is incomplete"
            )
        return owner, tenant, scopes

    @staticmethod
    def _validate_delegation_scope(scopes: frozenset[str], scope: str) -> None:
        accepted = {
            "kg:read": {"kg:read", "kg:write", "kg:admin"},
            "kg:write": {"kg:write", "kg:admin"},
        }.get(scope, {scope})
        if scopes.isdisjoint(accepted):
            raise mcp_protocol_exception(-32001, "Delegated task scope is insufficient")

    def _authorized_request_session(
        self,
        ctx: ServerRequestContext[Any, Any],
        params: Any,
        *,
        method: str,
        scope: str,
        route: Mapping[str, Any] | None,
    ) -> Any:
        service_authority = (
            self._authorized_delegator(ctx)
            if route is not None and "delegation" in route
            else None
        )
        delegated = self._delegated_session(
            route,
            method=method,
            params=params,
            scope=scope,
            service_authority=service_authority,
        )
        if delegated is not None:
            return delegated
        session = self._authorized_session(ctx, scope)
        self._validate_delegated_caller(route, session)
        return session

    async def _forward(
        self,
        method: str,
        params: Any,
        session: Any,
        route: Mapping[str, Any],
    ) -> Any:
        router = self._task_router
        if router is None or not callable(getattr(router, "forward_task_method", None)):
            raise mcp_protocol_exception(
                -32602,
                "Task belongs to another server and no owning-server route is available",
            )
        return await router.forward_task_method(
            method,
            params.model_dump(mode="json", by_alias=True, exclude_none=True),
            caller=self._caller_metadata(
                session,
                required_scope=("kg:read" if method == "tasks/get" else "kg:write"),
            ),
            route=route,
        )

    def _engine(self) -> Any:
        engine = self._engine_provider()
        if engine is None:
            raise mcp_protocol_exception(-32603, "IntelligenceGraphEngine not active.")
        return getattr(engine, "_work_item_engine", engine)

    async def _handle_get(
        self, ctx: ServerRequestContext[Any, Any], params: GetTaskParams
    ) -> GetTaskResult:
        self._require_tasks_capability(ctx)
        route = self._route_from_params(params)
        session = self._authorized_request_session(
            ctx, params, method="tasks/get", scope="kg:read", route=route
        )
        if route is not None and route["server"] != self.server_id:
            return await self._forward("tasks/get", params, session, route)
        return self._project(params.task_id, session=session, route=route)

    async def _handle_cancel(
        self, ctx: ServerRequestContext[Any, Any], params: GetTaskParams
    ) -> AckResult:
        self._require_tasks_capability(ctx)
        route = self._route_from_params(params)
        session = self._authorized_request_session(
            ctx, params, method="tasks/cancel", scope="kg:write", route=route
        )
        if route is not None and route["server"] != self.server_id:
            return await self._forward("tasks/cancel", params, session, route)

        item_id = self._owned_work_item_id(params.task_id, session)
        if not work_items.cancel_work_item(self._engine(), item_id):
            raise mcp_protocol_exception(-32602, "Failed to cancel task")
        current = work_items.get_work_item(self._engine(), item_id)
        if current is None:
            raise mcp_protocol_exception(-32602, "Task disappeared after cancellation")
        return self._ack_from_generic_item(
            params.task_id, current, session=session, route=route
        )

    def _owned_work_item_id(self, task_id: str, session: Any) -> str:
        """The work item behind ``task_id``; unknown unless the caller's tenant owns it."""
        item_id = _orchestrator_work_item_id(task_id)
        item = work_items.get_work_item(self._engine(), item_id)
        if item is None or item.get("tenant") != session.tenant:
            raise mcp_protocol_exception(-32602, "Unknown task")
        return item_id

    async def _handle_update(
        self, ctx: ServerRequestContext[Any, Any], params: UpdateTaskParams
    ) -> AckResult:
        self._require_tasks_capability(ctx)
        route = self._route_from_params(params)
        session = self._authorized_request_session(
            ctx, params, method="tasks/update", scope="kg:write", route=route
        )
        if route is not None and route["server"] != self.server_id:
            return await self._forward("tasks/update", params, session, route)

        self._owned_work_item_id(params.task_id, session)
        # The engine serves no input-response transition for a WorkItem, so an
        # input update is refused after the ownership check.
        raise mcp_protocol_exception(-32601, "Task input updates are not served")

    def _project(
        self,
        task_id: str,
        *,
        session: Any | None = None,
        route: Mapping[str, Any] | None = None,
    ) -> GetTaskResult:
        item_id = _orchestrator_work_item_id(task_id)
        item = work_items.get_work_item(self._engine(), item_id)
        if item is None:
            raise mcp_protocol_exception(-32602, "Unknown task")
        if session is not None and item.get("tenant") != session.tenant:
            raise mcp_protocol_exception(-32602, "Unknown task")
        raw_status = str(item.get("status") or "").lower()
        metadata = item.get("metadata")
        pending = (
            metadata.get("pending_input_request")
            if isinstance(metadata, dict)
            else None
        )
        status = self._map_work_item_status(raw_status, pending)
        result = GetTaskResult(
            task_id=task_id,
            status=status,
            created_at=_iso_timestamp(item.get("created_at")),
            last_updated_at=_iso_timestamp(item.get("updated_at")),
            # Native WorkItems have no automatic record-expiration policy: they
            # are durable graph records (mirrors the gateway's own ttlMs: null).
            ttl_ms=None,
            # `mcp_types.Result.meta` aliases to `_meta`; both spellings validate
            # at runtime (`populate_by_name=True`), but the pydantic mypy plugin
            # only recognizes the alias in the synthesized constructor signature.
            _meta=self._response_meta(session, route),
        )
        self._apply_task_status_payload(result, status, task_id, item, pending)
        return result

    @staticmethod
    def _map_work_item_status(raw_status: str, pending: Any) -> str:
        if raw_status in _WORKING_RAW_STATUSES and isinstance(pending, dict):
            return "input_required"
        if raw_status in _WORKING_RAW_STATUSES:
            return "working"
        if raw_status == "succeeded":
            return "completed"
        if raw_status in {"failed", "dead_letter"}:
            return "failed"
        if raw_status == "cancelled":
            return "cancelled"
        raise mcp_protocol_exception(-32603, "Unknown WorkItem status")

    def _completed_task_payload(
        self, task_id: str, item: Mapping[str, Any]
    ) -> dict[str, Any]:
        ref = item.get("result_ref")
        if ref is not None:
            return {"resultRef": ref}
        return {"status": "completed"}

    def _apply_task_status_payload(
        self,
        result: GetTaskResult,
        status: str,
        task_id: str,
        item: Mapping[str, Any],
        pending: Any,
    ) -> None:
        if status == "input_required":
            result.input_requests = {"request": pending}
        elif status == "completed":
            result.result = self._completed_task_payload(task_id, item)
        elif status == "failed":
            result.error = {"code": -32603, "message": "Arbora WorkItem failed"}

    def _ack_from_generic_item(
        self,
        task_id: str,
        item: Mapping[str, Any],
        *,
        session: Any,
        route: Mapping[str, Any] | None,
    ) -> AckResult:
        raw_status = str(item.get("status") or "").lower()
        if raw_status in _WORKING_RAW_STATUSES:
            status = "working"
        elif raw_status == "succeeded":
            status = "completed"
        elif raw_status in {"failed", "dead_letter"}:
            status = "failed"
        elif raw_status == "cancelled":
            status = "cancelled"
        else:
            raise mcp_protocol_exception(-32603, "Unknown WorkItem status")
        return AckResult(
            task_id=task_id,
            status=status,
            created_at=_iso_timestamp(item.get("created_at")),
            last_updated_at=_iso_timestamp(item.get("updated_at")),
            ttl_ms=None,
            _meta=self._response_meta(session, route),
        )
