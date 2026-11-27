"""Forwarding of MCP task methods to the child that owns the task (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import collections.abc as _collections_abc
import logging
import typing as _typing

import fastmcp.exceptions as _fastmcp_exceptions

from arbora.core.environment import read_env
from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    pass
import arbora.fleet.child_resilience as _child_resilience
from arbora.fleet.child_payload_bounds import (
    _child_required_scopes,
)
from arbora.fleet.fleet_capabilities import (
    require_fleet_capability,
)
from arbora.security.run_token import RunTokenSigner

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


#: The three native Tasks methods this gateway will route at all, and the
#: subset that MUTATES a child's task store (fenced harder, never retried).
_TASKS_METHODS = frozenset({"tasks/get", "tasks/update", "tasks/cancel"})


_TASKS_MUTATIONS = frozenset({"tasks/update", "tasks/cancel"})


class _TaskRoute(_typing.TypedDict):
    """The verified, immutable facts one native-Tasks request is routed on.

    Frozen at admission time so ``before_send`` can prove that the catalog
    generation, the child's connection generation, and (for stdio) its channel
    secret are all still the ones the route was authorized under.
    """

    method: str
    server: str
    mutation: bool
    is_remote: bool
    identity: dict[str, _typing.Any]
    params_type: _typing.Any
    result_type: _typing.Any
    admission_epoch: int
    runtime_generation: int | None
    admission_secret: str | None
    base_meta: dict[str, _typing.Any]


def _tasks_route_data(
    params: _collections_abc.Mapping[str, _typing.Any],
    route: _collections_abc.Mapping[str, _typing.Any] | None,
    extension_id: str,
) -> dict[str, _typing.Any]:
    """One Tasks request's owning-server route.

    An explicit ``route`` wins; otherwise it is read from the request's own
    ``_meta`` extension block.
    """
    route_data = dict(route or {})
    if route_data:
        return route_data
    raw_meta = params.get("_meta")
    raw_extension = (
        raw_meta.get(extension_id)
        if isinstance(raw_meta, _collections_abc.Mapping)
        else None
    )
    return (
        dict(raw_extension)
        if isinstance(raw_extension, _collections_abc.Mapping)
        else {}
    )


def _tasks_route_server(
    route_data: _collections_abc.Mapping[str, _typing.Any], revision: str
) -> str:
    """The validated owning-server name carried by one Tasks route."""
    server_name = route_data.get("server")
    if not isinstance(server_name, str) or not server_name.strip():
        raise _fastmcp_exceptions.ToolError("Tasks request has no owning-server route")
    if route_data.get("revision") != revision:
        raise _fastmcp_exceptions.ToolError(
            "Tasks owning-server route revision is unsupported"
        )
    return server_name.strip()


def _tasks_request_models(method: str) -> tuple[_typing.Any, _typing.Any]:
    """The ``(params, result)`` models for one Tasks method."""
    from arbora.fleet.tasks_wire import (
        AckResult,
        CancelTaskParams,
        GetTaskParams,
        GetTaskResult,
        UpdateTaskParams,
    )

    return {
        "tasks/get": (GetTaskParams, GetTaskResult),
        "tasks/update": (UpdateTaskParams, AckResult),
        "tasks/cancel": (CancelTaskParams, AckResult),
    }[method]


def _assert_tasks_caller_present(
    caller: _collections_abc.Mapping[str, _typing.Any] | None,
    route_data: _collections_abc.Mapping[str, _typing.Any],
) -> None:
    """A task delegation is only ever minted for a VERIFIED caller."""
    if isinstance(
        route_data.get("caller"), _collections_abc.Mapping
    ) and not isinstance(caller, _collections_abc.Mapping):
        raise _fastmcp_exceptions.ToolError("Tasks route caller is not verified")
    if not isinstance(caller, _collections_abc.Mapping):
        raise _fastmcp_exceptions.ToolError(
            "Authenticated task delegation is unavailable; use portable rm_jobs tools"
        )


def _assert_tasks_signing_secret(server_name: str) -> None:
    """Fail closed unless the shared task-delegation signing secret is present."""
    try:
        if not str(read_env("ARBORA_TOKEN_SECRET") or "").strip():
            raise RuntimeError("shared task-delegation signing secret is unavailable")
    except Exception as exc:
        logger.warning(
            "Tasks route delegation proof unavailable for child %s (%s)",
            server_name,
            type(exc).__name__,
            exc_info=True,
        )
        raise _fastmcp_exceptions.ToolError(
            "Authenticated task delegation is unavailable; use portable rm_jobs tools"
        ) from None


def _tasks_caller_identity(
    caller: _collections_abc.Mapping[str, _typing.Any],
) -> dict[str, _typing.Any]:
    """The verified tenant/owner/scopes a Tasks delegation is minted for."""
    from arbora.api.domain.mcp_server.backing.context_helpers import (
        caller_identity_fields,
    )

    identity = caller_identity_fields(caller)
    if not identity["tenant"] or not identity["owner"]:
        raise _fastmcp_exceptions.ToolError("Tasks route caller is not verified")
    return identity


def _build_task_request(
    signer: RunTokenSigner | None,
    route: _TaskRoute,
    params: _collections_abc.Mapping[str, _typing.Any],
    current_secret: str | None,
) -> _typing.Any:
    """One outgoing Tasks request: envelope, delegation proof, and channel proof."""
    from arbora.fleet.tasks_wire import (
        TASKS_EXTENSION_ID,
        TASKS_EXTENSION_REVISION,
        bound_signer,
        channel_proof,
        mint_delegation_token,
    )

    server_name = route["server"]
    try:
        delegation_token = mint_delegation_token(
            bound_signer(signer),
            route["method"],
            params,
            server=server_name,
            revision=TASKS_EXTENSION_REVISION,
            caller=route["identity"],
        )
    except Exception as exc:
        logger.warning(
            "Tasks route delegation proof unavailable for child %s (%s)",
            server_name,
            type(exc).__name__,
            exc_info=True,
        )
        raise _fastmcp_exceptions.ToolError(
            "Authenticated task delegation is unavailable; use portable rm_jobs tools"
        ) from None
    envelope: dict[str, _typing.Any] = {
        "server": server_name,
        "revision": TASKS_EXTENSION_REVISION,
        "caller": route["identity"],
        "delegation": {
            "issuer": "mcp-multiplexer",
            "token": delegation_token,
        },
    }
    if not route["is_remote"]:
        generation_secret = str(current_secret or "").strip()
        if not 32 <= len(generation_secret) <= 512:
            raise _fastmcp_exceptions.ToolError(
                "Authenticated stdio task channel is unavailable; "
                "use portable rm_jobs tools"
            )
        envelope["delegation"]["channel"] = channel_proof(
            generation_secret, delegation_token
        )
    outgoing = dict(params)
    outgoing_meta = dict(route["base_meta"])
    outgoing_meta[TASKS_EXTENSION_ID] = envelope
    outgoing["_meta"] = outgoing_meta
    parsed = route["params_type"].model_validate(outgoing)
    request_type = mcp_types.Request[route["params_type"], str]
    return request_type(method=route["method"], params=parsed)


def _assert_task_mutation_fence(
    route: _TaskRoute, current_generation: int, current_secret: str | None
) -> None:
    """The generation/channel fencing only a Tasks MUTATION must satisfy."""
    runtime_generation = route["runtime_generation"]
    if runtime_generation is not None and current_generation != runtime_generation:
        raise _fastmcp_exceptions.ToolError(
            "Tasks mutation connection generation changed before send"
        )
    if not route["is_remote"] and current_secret != route["admission_secret"]:
        raise _fastmcp_exceptions.ToolError(
            "Tasks mutation connection generation changed before send"
        )


class TaskForwarding:
    """Routes one task method to its owning child: capability checks, owner admission, route building and the request send."""

    def __init__(
        self,
        *,
        fleet: _typing.Any,
        signer: _typing.Any,
        children: _typing.Any,
        catalog_epoch: _typing.Any,
        load_catalog: _typing.Any,
        mount_child: _typing.Any,
    ) -> None:
        self._fleet = fleet
        self._signer = signer
        self.children = children
        self._catalog_epoch = catalog_epoch
        self.load_catalog = load_catalog
        self.mount_child = mount_child

    def _task_route_retired(self, route: _TaskRoute, runtime: _typing.Any) -> bool:
        """True when the child pool this route was admitted against is gone.

        ``live is None`` is checked explicitly rather than relying on the identity
        test alone: if the child had been REMOVED, ``children.get`` returns None,
        and a ``runtime`` that is also None would have compared equal and let a
        retired route through. Checking the looked-up value rather than the
        caller's reference is also what proves it non-None for the capability
        check.
        """
        server_name = route["server"]
        live = self.children.get(server_name)
        return (
            self._catalog_epoch() != route["admission_epoch"]
            or live is None
            or live is not runtime
            or not self._tasks_runtime_capable(server_name, live)
        )

    def _assert_task_route_current(
        self,
        route: _TaskRoute,
        runtime: _typing.Any,
        current_generation: int,
        current_secret: str | None,
    ) -> None:
        """Revalidate one Tasks route immediately before its request is sent."""
        mutation = route["mutation"]
        if self._task_route_retired(route, runtime):
            raise _fastmcp_exceptions.ToolError(
                "Tasks mutation route was retired before the request was sent"
                if mutation
                else "Tasks read route was retired before the request was sent"
            )
        if not route["is_remote"] and not 32 <= len(str(current_secret or "")) <= 512:
            raise _fastmcp_exceptions.ToolError(
                "Authenticated stdio task channel is unavailable; "
                "use portable rm_jobs tools"
            )
        if mutation:
            _assert_task_mutation_fence(route, current_generation, current_secret)

    @staticmethod
    def _tasks_child_capable(initialization: _typing.Any) -> bool:
        """Return whether one initialize result advertises this Tasks revision."""

        from arbora.fleet.tasks_wire import TASKS_EXTENSION_ID, TASKS_EXTENSION_REVISION

        capabilities = getattr(initialization, "capabilities", None)
        extensions = getattr(capabilities, "extensions", None)
        if not isinstance(extensions, _collections_abc.Mapping):
            return False
        settings = extensions.get(TASKS_EXTENSION_ID)
        return isinstance(settings, _collections_abc.Mapping) and (
            settings.get("revision") == TASKS_EXTENSION_REVISION
        )

    def _tasks_runtime_capable(
        self, server_name: str, runtime: _child_resilience.ChildRuntime
    ) -> bool:
        """Require every selectable session in one child pool to qualify."""

        sessions = getattr(runtime, "_sessions", None)
        if not isinstance(sessions, list) or not sessions:
            return False
        # The live pool is the sole capability authority. Every selectable
        # session is interrogated directly so a rolling/mixed replica cannot
        # inherit one last-writer handshake or a retired generation's record.
        return all(
            self._tasks_child_capable(getattr(session, "initialize_result", None))
            for session in sessions
        )

    async def forward_task_method(
        self,
        method: str,
        params: _collections_abc.Mapping[str, _typing.Any],
        *,
        caller: _collections_abc.Mapping[str, _typing.Any] | None = None,
        route: _collections_abc.Mapping[str, _typing.Any] | None = None,
    ) -> _typing.Any:
        """Forward one native Tasks request to its owning child server.

        This is a request router, not a task store.  The child WorkItem
        authority answers the request and the existing ``_child_resilience.ChildRuntime``
        supplies bounded queueing, restart/retry, and replica-session
        selection. Capability negotiation is checked against the child's
        latest initialize result before any task ID crosses the route.
        """

        from arbora.fleet.tasks_wire import TASKS_EXTENSION_ID, TASKS_EXTENSION_REVISION

        if method not in _TASKS_METHODS:
            raise _fastmcp_exceptions.ToolError("Unsupported Tasks method")
        if not isinstance(params, _collections_abc.Mapping):
            raise _fastmcp_exceptions.ToolError("Tasks request parameters are invalid")
        route_data = _tasks_route_data(params, route, TASKS_EXTENSION_ID)
        server_name = _tasks_route_server(route_data, TASKS_EXTENSION_REVISION)
        catalog = self.load_catalog()
        if server_name not in catalog:
            raise _fastmcp_exceptions.ToolError(
                "Tasks owning server is not in the active catalog"
            )
        # Apply the same fleet/delegation authorization used by tool
        # forwarding before a task poll can lazily spawn a child.
        require_fleet_capability("delegate", self._fleet.config)
        require_fleet_capability(
            "delegate", self._fleet.config, _child_required_scopes(catalog[server_name])
        )

        runtime = await self._admit_tasks_owner(server_name)
        _assert_tasks_caller_present(caller, route_data)
        task_route = self._build_task_route(
            method,
            params,
            _typing.cast("_collections_abc.Mapping[str, _typing.Any]", caller),
            server_name,
            runtime,
        )
        return await self._send_task_request(params, task_route, runtime)

    async def _admit_tasks_owner(self, server_name: str) -> _typing.Any:
        """Mount (once) and verify the child that owns this task.

        Lazy task polling is allowed to mount the owner exactly once, just as
        lazy tool loading does. No process-local task state is created.
        """
        if server_name not in self.children:
            await self.mount_child(server_name)
        runtime = self.children.get(server_name)
        if runtime is None:
            raise _fastmcp_exceptions.ToolError("Tasks owning server is unavailable")
        if not self._tasks_runtime_capable(server_name, runtime):
            raise _fastmcp_exceptions.ToolError(
                "Tasks owning server did not advertise native Tasks"
            )
        return runtime

    def _build_task_route(
        self,
        method: str,
        params: _collections_abc.Mapping[str, _typing.Any],
        caller: _collections_abc.Mapping[str, _typing.Any],
        server_name: str,
        runtime: _typing.Any,
    ) -> _TaskRoute:
        """Freeze the verified, immutable facts this Tasks request is routed on.

        Capability negotiation is already checked by the caller; this captures
        the exact catalog generation, connection generation, and channel secret
        the route was admitted under, so :func:`_assert_task_route_current` can
        later prove none of them moved before the request was actually sent.
        """
        runtime_generation = getattr(runtime, "generation", None)
        if not isinstance(runtime_generation, int):
            runtime_generation = None
        params_type, result_type = _tasks_request_models(method)
        raw_meta = params.get("_meta")
        child_cfg = self.load_catalog()[server_name]
        explicit_transport = str(child_cfg.get("transport", "")).lower()
        is_remote = bool(child_cfg.get("url")) or explicit_transport in {
            "streamable-http",
            "sse",
        }
        _assert_tasks_signing_secret(server_name)
        return {
            "method": method,
            "server": server_name,
            "mutation": method in _TASKS_MUTATIONS,
            "is_remote": is_remote,
            "identity": _tasks_caller_identity(caller),
            "params_type": params_type,
            "result_type": result_type,
            "admission_epoch": self._catalog_epoch(),
            "runtime_generation": runtime_generation,
            "admission_secret": getattr(runtime, "_task_generation_secret", None),
            "base_meta": dict(raw_meta)
            if isinstance(raw_meta, _collections_abc.Mapping)
            else {},
        }

    async def _send_task_request(
        self,
        params: _collections_abc.Mapping[str, _typing.Any],
        route: _TaskRoute,
        runtime: _typing.Any,
    ) -> _typing.Any:
        """Send one admitted Tasks request through the child's bounded runtime.

        Reads may retry once, but each attempt still revalidates the owning
        catalog/runtime and exact Tasks revision through ``before_send``.
        Mutations add generation fencing and disable retry.
        """
        parsed_input = route["params_type"].model_validate(dict(params))

        call_request = getattr(runtime, "call_request", None)
        if not callable(call_request):
            raise _fastmcp_exceptions.ToolError(
                "Tasks owning server has no bounded request runtime"
            )
        mutation = route["mutation"]

        def _factory(
            _current_generation: int, current_secret: str | None
        ) -> _typing.Any:
            return _build_task_request(self._signer, route, params, current_secret)

        def _before_send(current_generation: int, current_secret: str | None) -> None:
            self._assert_task_route_current(
                route, runtime, current_generation, current_secret
            )

        result = await call_request(
            _factory(route["runtime_generation"] or 0, route["admission_secret"])
            if mutation
            else None,
            route["result_type"],
            retry_on_transient=not mutation,
            generation_marker=route["runtime_generation"] if mutation else None,
            request_factory=None if mutation else _factory,
            before_send=_before_send,
        )
        if not isinstance(result, route["result_type"]):
            result = route["result_type"].model_validate(result)
        task_id = getattr(parsed_input, "task_id", None)
        returned_id = getattr(result, "task_id", None)
        if task_id and returned_id and returned_id != task_id:
            raise _fastmcp_exceptions.ToolError(
                "Tasks owning server returned a mismatched task ID"
            )
        return result
