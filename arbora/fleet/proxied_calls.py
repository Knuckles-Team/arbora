"""Proxied tool calls: admission, schema disclosure and OAuth-gated calls (ARBORA-HOST-R038.9)."""

from __future__ import annotations

import asyncio
import collections.abc as _collections_abc
import contextlib
import json
import logging
import typing as _typing

import fastmcp.exceptions as _fastmcp_exceptions

from arbora.fleet.protocol_compat import mcp_types_module

if _typing.TYPE_CHECKING:
    from mcp_types import CallToolResult as MCPCallToolResult
import arbora.fleet.child_resilience as _child_resilience
from arbora.api.domain.fleet.approval_meta import approval_call_kwargs
from arbora.fleet.child_payload_bounds import (
    _child_error_result,
    _child_failure_result,
    _child_required_scopes,
    _classified_child_error,
    _list_all_tools,
    assert_bounded_delegated_value,
)
from arbora.fleet.child_runtime_policy import (
    _close_runtime_child_policy,
    _prepare_runtime_child_policy,
)
from arbora.fleet.fleet_capabilities import (
    require_fleet_capability,
)
from arbora.fleet.oauth_brokers import oauth_gated
from arbora.fleet.session_keys import (
    ambient_session_key,
)
from arbora.security.error_surface import public_error_text
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


#: A child call's deadline when neither the call nor the server declares one
#: (ARBORA-FLEET-R075); the same default the pooled ``ChildRuntime`` uses.
_DEFAULT_CHILD_CALL_TIMEOUT_S = 300.0


def _child_call_timeout(cfg: dict, timeout: float | None) -> float:
    """One ephemeral child call's deadline in seconds; ``0.0`` when the
    declaration is unusable (the caller rejects anything outside its boundary).

    ARBORA-FLEET-R075: the call's own deadline, else the server's declared
    ``call_timeout``, else the default. The server's ``timeout`` is its
    connect/handshake budget and never caps a call: a tool that legitimately
    takes a minute must not fail at a 15 s handshake budget.
    """

    value = timeout if timeout is not None else cfg.get("call_timeout")
    try:
        return float(_DEFAULT_CHILD_CALL_TIMEOUT_S if value is None else value)
    except (TypeError, ValueError):
        return 0.0


class ProxiedCalls:
    """Admits and sends one proxied tool call to its child: catalog and workflow admission, schema disclosure and the OAuth-gated session call."""

    def __init__(
        self,
        *,
        child_policy_admitted_tools: _typing.Any,
        child_runtime_policies: _typing.Any,
        child_schema_refresh_errors: _typing.Any,
        child_schema_revisions: _typing.Any,
        fleet: _typing.Any,
        children: _typing.Any,
        workflow_bindings: _typing.Any,
        catalog: _typing.Any,
        catalog_epoch: _typing.Any,
        host_mcp: _typing.Any,
        aggregated_tools: _typing.Any,
        credential_authority: _typing.Any,
        tool_to_server: _typing.Any,
        admit_runtime_policy_tools: _typing.Any,
        open_one_session: _typing.Any,
        load_catalog: _typing.Any,
        notify_pending_tools_changed: _typing.Any,
        tool_dispatchable: _typing.Any,
    ) -> None:
        self._child_policy_admitted_tools = child_policy_admitted_tools
        self._child_runtime_policies = child_runtime_policies
        self._child_schema_refresh_errors = child_schema_refresh_errors
        self._child_schema_revisions = child_schema_revisions
        self._fleet = fleet
        self.children = children
        self.workflow_bindings = workflow_bindings
        self._catalog = catalog
        self._catalog_epoch = catalog_epoch
        self._host_mcp = host_mcp
        self.aggregated_tools = aggregated_tools
        self.credential_authority = credential_authority
        self.tool_to_server = tool_to_server
        self._admit_runtime_policy_tools = admit_runtime_policy_tools
        self._open_one_session = open_one_session
        self.load_catalog = load_catalog
        self.notify_pending_tools_changed = notify_pending_tools_changed
        self.tool_dispatchable = tool_dispatchable

    def _admit_proxied_call(
        self, prefixed_name: str
    ) -> tuple[str, str, dict, _typing.Any]:
        """Resolve and authorize one proxied call.

        Returns ``(server_name, original_name, child_config, runtime)``; raises
        if the tool/server is unknown, disabled, inactive, or not admitted by
        the child's runtime policy.
        """
        if prefixed_name not in self.tool_to_server():
            raise ValueError("_fastmcp_tools.Tool is not registered in multiplexer")

        server_name, original_name = self.tool_to_server()[prefixed_name]
        child_config = self.load_catalog().get(server_name)
        if child_config is None:
            raise _fastmcp_exceptions.ToolError("MCP child is no longer enabled")
        require_fleet_capability(
            "delegate", self._fleet.config, _child_required_scopes(child_config)
        )
        runtime = self.children.get(server_name)
        if runtime is None:
            raise RuntimeError("MCP child session is not active")
        policy = self._child_runtime_policies.get(server_name)
        if (
            policy is not None
            and original_name
            not in self._child_policy_admitted_tools.get(server_name, frozenset())
        ):
            raise _fastmcp_exceptions.ToolError(
                "MCP child tool is not admitted by runtime policy"
            )
        return server_name, original_name, child_config, runtime

    @staticmethod
    def _schema_disclosure_for_tool(
        descriptor: _typing.Any, original_name: str
    ) -> tuple[_typing.Any, _typing.Any] | None:
        """Select one SDK-validated manifest from the admitted tool metadata."""
        metadata = getattr(descriptor, "meta", None)
        raw_manifest = (
            metadata.get("openapi_schema")
            if isinstance(metadata, _collections_abc.Mapping)
            else None
        )
        if raw_manifest is None:
            if original_name.startswith("operation_"):
                raise _fastmcp_exceptions.ToolError(
                    "OpenAPI operation schema disclosure is missing"
                )
            return None
        try:
            from scion_sdk.mcp.schema_disclosure import SchemaManifest
        except ImportError:
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema disclosure contract is unavailable"
            ) from None
        try:
            manifest = SchemaManifest.model_validate(raw_manifest)
        except (TypeError, ValueError):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI operation schema disclosure is invalid"
            ) from None
        if manifest.alias != original_name:
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI operation schema identity does not match the admitted tool"
            )
        return SchemaManifest, manifest

    @staticmethod
    def _schema_resource_payload(result: _typing.Any) -> dict[str, _typing.Any]:
        """Read one bounded JSON text resource without trusting provider output."""
        contents = getattr(result, "contents", None)
        if not isinstance(contents, (list, tuple)) or len(contents) != 1:
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema resource has an invalid content envelope"
            )
        item = contents[0]
        mime_type = getattr(item, "mime_type", getattr(item, "mimeType", None))
        text = getattr(item, "text", None)
        if mime_type != "application/json" or not isinstance(text, str):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema resource is not JSON text"
            )
        if len(text.encode("utf-8")) > 32 * 1024:
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema resource exceeds its envelope limit"
            )
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema resource is not valid JSON"
            ) from None
        if not isinstance(payload, dict):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema resource has an invalid document"
            )
        return payload

    async def _preflight_openapi_schema(
        self,
        session: _typing.Any,
        manifest_model: _typing.Any,
        manifest: _typing.Any,
        descriptor: _typing.Any,
        arguments: dict[str, _typing.Any],
        before_read: _typing.Callable[[], None],
    ) -> None:
        """Fetch and reconstruct the selected schema through this child session."""
        from scion_sdk.mcp.schema_disclosure import reconstruct_schema

        before_read()
        manifest_result = await session.read_resource(manifest.manifest_uri)
        remote_manifest_payload = self._schema_resource_payload(manifest_result)
        try:
            remote_manifest = manifest_model.model_validate(remote_manifest_payload)
        except (TypeError, ValueError):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema manifest is invalid"
            ) from None
        if remote_manifest.model_dump() != manifest.model_dump():
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema manifest no longer matches the selected tool"
            )

        fragments = []
        for index in range(manifest.fragment_count):
            before_read()
            uri = manifest.fragment_uri_template.format(index=index)
            result = await session.read_resource(uri)
            fragments.append(self._schema_resource_payload(result))
        try:
            reconstructed = reconstruct_schema(manifest.model_dump(), fragments)
        except (TypeError, ValueError):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI schema fragments failed complete reconstruction"
            ) from None

        self._assert_reconstructed_descriptor(reconstructed, descriptor)
        self._assert_disclosed_action(
            reconstructed["inputSchema"], manifest.operation, arguments
        )

    @staticmethod
    def _assert_reconstructed_descriptor(
        reconstructed: dict[str, _typing.Any], descriptor: _typing.Any
    ) -> None:
        """Require both reconstructed schemas to equal the admitted descriptor."""
        input_schema = getattr(descriptor, "input_schema", None)
        output_schema = getattr(descriptor, "output_schema", None)
        if (
            not isinstance(input_schema, dict)
            or reconstructed["inputSchema"] != input_schema
            or not isinstance(output_schema, dict)
            or reconstructed["outputSchema"] != output_schema
        ):
            raise _fastmcp_exceptions.ToolError(
                "Reconstructed OpenAPI schema differs from the admitted tool"
            )

    @staticmethod
    def _assert_disclosed_action(
        input_schema: dict[str, _typing.Any],
        operation: str,
        arguments: dict[str, _typing.Any],
    ) -> None:
        """Bind the sole schema action to the manifest and requested operation."""
        branches = input_schema.get("oneOf")
        if not isinstance(branches, list) or len(branches) != 1:
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI operation schema is not bound to one action"
            )
        action_schema = (
            branches[0].get("properties", {}).get("action", {})
            if isinstance(branches[0], dict)
            else {}
        )
        if (
            not isinstance(action_schema, dict)
            or (
                action_schema.get("enum") != [operation]
                and action_schema.get("const") != operation
            )
            or arguments.get("action") != operation
        ):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI operation does not match the requested action"
            )

    def _proxied_tool_allows_transient_retry(self, prefixed_name: str) -> bool:
        """Retry only a currently admitted tool whose effect is explicitly read-only."""
        descriptor = next(
            (tool for tool in self.aggregated_tools() if tool.name == prefixed_name),
            None,
        )
        if descriptor is None:
            return False
        try:
            from arbora.api.registry import Effect
            from arbora.fleet.gateway_ops import annotation_effect

            effect, _, _ = annotation_effect(getattr(descriptor, "annotations", None))
        except (TypeError, ValueError):
            return False
        return effect is Effect.READ

    def _workflow_refusal(
        self, prefixed_name: str, session_key: str | None = None
    ) -> str | None:
        """The workflow binding refusal for a tool in a session (ARBORA-FLEET-R021.1)."""
        key = ambient_session_key() if session_key is None else session_key
        return self.workflow_bindings.refusal(prefixed_name, key)

    def _require_workflow_binding(self, prefixed_name: str) -> None:
        refusal = self._workflow_refusal(prefixed_name)
        if refusal is not None:
            raise _fastmcp_exceptions.ToolError(refusal)

    def _select_proxied_disclosure(
        self, prefixed_name: str, original_name: str
    ) -> tuple[_typing.Any, _typing.Any]:
        """Select the currently visible descriptor and its disclosure contract."""
        self._require_workflow_binding(prefixed_name)
        if not self.tool_dispatchable(prefixed_name):
            raise _fastmcp_exceptions.ToolError(
                "MCP child tool is not loaded for this session"
            )
        descriptor = next(
            (tool for tool in self.aggregated_tools() if tool.name == prefixed_name),
            None,
        )
        if descriptor is None and original_name.startswith("operation_"):
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI operation is not in the admitted child catalog"
            )
        selected_disclosure = (
            self._schema_disclosure_for_tool(descriptor, original_name)
            if descriptor is not None
            else None
        )

        return descriptor, selected_disclosure

    def _assert_proxied_catalog(
        self,
        prefixed_name: str,
        server_name: str,
        original_name: str,
        runtime: _typing.Any,
        epoch: int,
        revision: int,
    ) -> None:
        """Recheck the catalog facts captured when this call was admitted."""
        if (
            not self.tool_dispatchable(prefixed_name)
            or self._catalog_epoch() != epoch
            or self.children.get(server_name) is not runtime
            or self.tool_to_server().get(prefixed_name) != (server_name, original_name)
            or server_name in self._child_schema_refresh_errors
            or self._child_schema_revisions.get(server_name, 0) != revision
        ):
            raise _fastmcp_exceptions.ToolError(
                "MCP child admission changed before dispatch"
            )

    def _proxied_admission_checker(
        self,
        prefixed_name: str,
        server_name: str,
        original_name: str,
        child_config: dict,
        runtime: _typing.Any,
        epoch: int,
        revision: int,
        generation: int,
        schema_bound: bool,
    ) -> _typing.Callable[[int, str | None], None]:
        """Capture generation and authorization checks for queue and preflight waits."""

        def recheck(current_generation: int, _secret: str | None) -> None:
            require_fleet_capability(
                "delegate", self._fleet.config, _child_required_scopes(child_config)
            )
            if runtime.generation != current_generation or (
                schema_bound and runtime.generation != generation
            ):
                raise _fastmcp_exceptions.ToolError(
                    "MCP child admission changed before dispatch"
                )
            self._assert_proxied_catalog(
                prefixed_name, server_name, original_name, runtime, epoch, revision
            )

        return recheck

    def _make_schema_preflight(
        self,
        selected: _typing.Any,
        descriptor: _typing.Any,
        arguments: dict[str, _typing.Any],
        before_read: _typing.Callable[[], None],
    ) -> _typing.Callable[[_typing.Any], _typing.Awaitable[None]] | None:
        """Use the selected session for every read of the chosen disclosure."""
        if selected is None:
            return None
        manifest_model, manifest = selected

        async def preflight(session: _typing.Any) -> None:
            await self._preflight_openapi_schema(
                session, manifest_model, manifest, descriptor, arguments, before_read
            )

        return preflight

    async def _dispatch_proxied_call(
        self,
        prefixed_name: str,
        server_name: str,
        original_name: str,
        child_config: dict,
        runtime: _typing.Any,
        arguments: dict[str, _typing.Any],
    ) -> MCPCallToolResult:
        """Forward one admitted call through the child's hardened runtime
        (per-server concurrency limit + bounded queue)."""
        catalog_epoch_before = self._catalog_epoch()
        revision_before = self._child_schema_revisions.get(server_name, 0)
        generation_before = runtime.generation
        descriptor, selected_disclosure = self._select_proxied_disclosure(
            prefixed_name, original_name
        )
        recheck_admission = self._proxied_admission_checker(
            prefixed_name,
            server_name,
            original_name,
            child_config,
            runtime,
            catalog_epoch_before,
            revision_before,
            generation_before,
            selected_disclosure is not None,
        )
        preflight = self._make_schema_preflight(
            selected_disclosure,
            descriptor,
            arguments,
            lambda: recheck_admission(runtime.generation, None),
        )
        result = await runtime.call_tool(
            original_name,
            arguments,
            retry_on_transient=(
                selected_disclosure is None
                and self._proxied_tool_allows_transient_retry(prefixed_name)
            ),
            generation_marker=generation_before,
            before_send=recheck_admission,
            preflight=preflight,
        )
        return await self._finalize_proxied_result(server_name, revision_before, result)

    async def _finalize_proxied_result(
        self, server_name: str, revision_before: int, result: MCPCallToolResult
    ) -> MCPCallToolResult:
        """Refuse failed refreshes and deliver schema changes on this live request."""
        # A reconnect can complete while the call above is waiting for the
        # runtime's ready gate.  Do not let that freshly recovered child
        # serve through a route whose outer schema could not be refreshed.
        if server_name in self._child_schema_refresh_errors:
            return _child_error_result("schema_refresh_failed")
        if (
            self._host_mcp() is not None
            and self._child_schema_revisions.get(server_name, 0) != revision_before
        ):
            # This call supplied the request context that observed the
            # reconnect.  A detached recovery queues a durable revision;
            # use this live request to deliver it to this session.
            await self.notify_pending_tools_changed()
        return result

    async def call_proxied_tool(
        self, prefixed_name: str, arguments: dict[str, _typing.Any] | None = None
    ) -> MCPCallToolResult:
        """Forward a prefixed tool call to the owning child server's session.

        Looks up the ``(server_name, original_name)`` mapping recorded during
        :meth:`start_children` and forwards the call to that child's live
        ``ClientSession``. Raises if the tool/server is unknown or inactive.
        """
        require_fleet_capability("delegate", self._fleet.config)
        logger.info("Calling delegated MCP tool")
        call_arguments = arguments or {}
        assert_bounded_delegated_value(call_arguments)
        server_name, original_name, child_config, runtime = self._admit_proxied_call(
            prefixed_name
        )
        if server_name in self._child_schema_refresh_errors:
            return _child_error_result("schema_refresh_failed")

        try:
            return await self._dispatch_proxied_call(
                prefixed_name,
                server_name,
                original_name,
                child_config,
                runtime,
                call_arguments,
            )
        except _child_resilience.MCPChildError as e:
            # Typed per-child failure (busy/restarting/failed/circuit-open):
            # the CALLER-facing result is deliberately just the class name (so
            # callers can branch on it) — but the server-side log keeps the
            # full message (e.g. which server, timing), which the class name
            # alone drops.
            logger.warning(
                "Child tool call rejected: %s: %s",
                type(e).__name__,
                redact_for_log(e),
            )
            return _child_error_result(type(e).__name__)
        except Exception as e:
            logger.warning("call_proxied_tool failed", exc_info=True)
            return _child_error_result(public_error_text(e))

    async def call_admitted_child_once(
        self,
        server_name: str,
        original_name: str,
        arguments: dict[str, _typing.Any] | None = None,
        *,
        timeout: float | None = None,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """Make exactly one governed child call over a dedicated session.

        The one call path the governed fleet gateway uses. An OAuth-gated
        child is called with the current caller's own grant
        (:meth:`call_oauth_gated_tool`). Any other child is called as Arbora
        itself: the session carries the child's configured static headers and
        Arbora's service credential, never a caller credential. Either way
        the session never joins the shared pool and makes one attempt only.
        The caller's own admission has already passed the invocation
        pipeline; the child capability is re-checked here.
        """
        cfg = self.load_catalog().get(server_name)
        if cfg is not None and oauth_gated(cfg):
            return await self.call_oauth_gated_tool(
                server_name,
                original_name,
                arguments,
                timeout=timeout,
                approval_ref=approval_ref,
            )
        return await self._call_service_child_once(
            server_name,
            original_name,
            arguments,
            timeout=timeout,
            approval_ref=approval_ref,
        )

    async def _call_service_child_once(
        self,
        server_name: str,
        original_name: str,
        arguments: dict[str, _typing.Any] | None,
        *,
        timeout: float | None,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        require_fleet_capability("delegate", self._fleet.config)
        cfg = self.load_catalog().get(server_name)
        if cfg is None:
            # Nothing was sent to the child, so the outcome is certain: the
            # child is not mountable (disabled, or refused at catalog
            # admission). Answer UNAVAILABLE, never an unsettled outcome.
            return _classified_child_error(
                "UNAVAILABLE", "child is not mountable in the current catalog"
            )
        if oauth_gated(cfg):
            raise _fastmcp_exceptions.ToolError(
                "Server is not a configured service-credential MCP child"
            )
        require_fleet_capability(
            "delegate", self._fleet.config, _child_required_scopes(cfg)
        )
        catalog_epoch = self._catalog_epoch()
        call_arguments = arguments or {}
        assert_bounded_delegated_value(call_arguments)
        call_timeout = _child_call_timeout(cfg, timeout)
        if not 0.001 <= call_timeout <= 300.0:
            raise _fastmcp_exceptions.ToolError("Invalid call timeout")
        try:
            return await self._admitted_ephemeral_call(
                server_name,
                original_name,
                cfg,
                call_arguments,
                catalog_epoch,
                approval_ref,
                call_timeout,
            )
        except TimeoutError:
            return _child_error_result("MCPChildTimeout")
        except Exception as e:
            logger.warning("_call_service_child_once failed", exc_info=True)
            return _child_failure_result(server_name, original_name, e)

    async def _admitted_ephemeral_call(
        self,
        server_name: str,
        original_name: str,
        cfg: dict,
        arguments: dict[str, _typing.Any],
        catalog_epoch: int,
        approval_ref: str,
        call_timeout: float,
    ) -> MCPCallToolResult:
        """One ephemeral child call, under the credential grant the child declares."""
        from arbora.fleet.credential_admission import (
            CredentialRefusal,
            credentialed_call,
        )

        try:
            async with credentialed_call(
                self.credential_authority(), server_name, original_name, cfg
            ):
                return await asyncio.wait_for(
                    self._call_ephemeral_child_session(
                        server_name,
                        original_name,
                        cfg,
                        arguments,
                        catalog_epoch,
                        approval_ref,
                    ),
                    timeout=call_timeout,
                )
        except CredentialRefusal as refused:
            return _classified_child_error(
                "PERMISSION_DENIED", f"credential access denied: {refused.code}"
            )

    async def call_oauth_gated_tool(
        self,
        server_name: str,
        original_name: str,
        arguments: dict[str, _typing.Any] | None = None,
        *,
        timeout: float | None = None,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """Per-principal tool call for an OAuth-gated remote MCP child (NE-008,
        GOC-85 Deliverable 3 — U-44/U-45 wiring).

        :meth:`call_proxied_tool` forwards to ``self.children[server_name]`` —
        ONE shared session for every caller, admitted at pool-mount time. An
        OAuth-gated child is never pool-mounted (:meth:`_start_child` skips
        it), precisely because that sharing is unsafe for a per-user grant.
        This method is its call path instead: open a dedicated, ephemeral
        session bound to the CURRENT caller's own delegated grant
        (:meth:`RemoteOAuthBrokers.bearer`, inside
        :meth:`_open_one_session`), make exactly one tool call over it, and
        tear the session down — never joining the shared pool, never caching
        anything keyed only by server name. A caller without a valid grant for
        this provider gets a fail-closed error before any tool call is
        attempted (the connect step inside :meth:`_open_one_session` raises,
        because :meth:`~arbora.fleet.remote_oauth_broker.RemoteOAuthBroker.bearer_headers_for`
        does).

        Fleet-global discoverability (this call reachable through
        :meth:`call_proxied_tool`'s prefixed-name aggregation, or surfaced by
        ``find_tools``/``load_tools``) is NOT built here — that catalog is
        principal-agnostic by construction (one aggregated view for every
        caller) and extending it to vary per principal is GOC-15/catalog-layer
        scope, not this track's. A caller that already knows the
        ``server_name``/``original_name`` (e.g. from its own
        :meth:`probe_server` call, which DOES run live and per-principal for
        an OAuth-gated server) can call this directly today.
        """
        require_fleet_capability("delegate", self._fleet.config)
        cfg = self.load_catalog().get(server_name)
        if cfg is None or not oauth_gated(cfg):
            raise _fastmcp_exceptions.ToolError(
                "Server is not a configured OAuth-gated remote MCP child"
            )
        require_fleet_capability(
            "delegate", self._fleet.config, _child_required_scopes(cfg)
        )
        catalog_epoch = self._catalog_epoch()
        call_arguments = arguments or {}
        assert_bounded_delegated_value(call_arguments)
        call_timeout = _child_call_timeout(cfg, timeout)
        if not 0.001 <= call_timeout <= 300.0:
            raise _fastmcp_exceptions.ToolError("Invalid call timeout")

        try:
            return await asyncio.wait_for(
                self._call_ephemeral_oauth_session(
                    server_name,
                    original_name,
                    cfg,
                    call_arguments,
                    catalog_epoch,
                    approval_ref,
                ),
                timeout=call_timeout,
            )
        except TimeoutError:
            return _child_error_result("MCPChildTimeout")
        except Exception as e:
            logger.warning("call_oauth_gated_tool failed", exc_info=True)
            return _child_failure_result(server_name, original_name, e)

    async def _assert_oauth_tool_admitted(
        self,
        server_name: str,
        original_name: str,
        session: _typing.Any,
        runtime_policy: _typing.Any,
    ) -> _typing.Any | None:
        """Refuse a tool this child's runtime policy does not admit.

        ``record_state=False``: an ephemeral per-principal session must never
        write into the process-wide admitted-tool map.
        """
        tools = await _list_all_tools(session)
        admitted = self._oauth_admitted_tools(
            server_name, original_name, runtime_policy, tools
        )
        return self._find_tool_descriptor(admitted, original_name)

    @staticmethod
    def _find_tool_descriptor(
        tools: list[_typing.Any], name: str
    ) -> _typing.Any | None:
        """Find the exact advertised descriptor for one original child tool name."""
        return next((tool for tool in tools if tool.name == name), None)

    def _oauth_admitted_tools(
        self,
        server_name: str,
        original_name: str,
        runtime_policy: _typing.Any,
        tools: list[_typing.Any],
    ) -> list[_typing.Any]:
        """Apply policy without modifying the shared session's admitted-tool map."""
        if runtime_policy is None:
            return tools
        admitted = self._admit_runtime_policy_tools(
            server_name, runtime_policy, tools, record_state=False
        )
        if original_name not in {tool.name for tool in admitted}:
            raise _fastmcp_exceptions.ToolError(
                "MCP child tool is not admitted by runtime policy"
            )
        return admitted

    def _assert_oauth_catalog(self, server_name: str, cfg: dict, epoch: int) -> None:
        """Reauthorize the principal and refuse a changed child configuration."""
        require_fleet_capability(
            "delegate", self._fleet.config, _child_required_scopes(cfg)
        )
        if (
            self._catalog_epoch() != epoch
            or self._catalog() is None
            or self._catalog().get(server_name) is not cfg
        ):
            raise _fastmcp_exceptions.ToolError(
                "MCP child admission changed before dispatch"
            )

    async def _call_ephemeral_child_session(
        self,
        server_name: str,
        original_name: str,
        cfg: dict,
        arguments: dict[str, _typing.Any],
        catalog_epoch: int,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """One call over a dedicated service-credential session (no OAuth).

        The same admission, schema and dispatch checks as the OAuth path
        (:meth:`_invoke_oauth_tool`) run on the dedicated session.
        """
        self._assert_oauth_catalog(server_name, cfg, catalog_epoch)
        runtime_cfg, runtime_policy = _prepare_runtime_child_policy(
            cfg, self._fleet.config
        )
        try:
            async with contextlib.AsyncExitStack() as stack:
                session = await self._open_one_session(server_name, runtime_cfg, stack)
                return await self._invoke_oauth_tool(
                    session,
                    server_name,
                    original_name,
                    cfg,
                    arguments,
                    catalog_epoch,
                    runtime_policy,
                    approval_ref,
                )
        finally:
            if runtime_policy is not None:
                _close_runtime_child_policy(runtime_policy)

    async def _call_ephemeral_oauth_session(
        self,
        server_name: str,
        original_name: str,
        cfg: dict,
        arguments: dict[str, _typing.Any],
        catalog_epoch: int,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """One tool call over a dedicated session bound to THIS caller's grant.

        The session never joins the shared pool and nothing is cached keyed
        only by server name, so a per-user OAuth grant is never shared.
        """
        self._assert_oauth_catalog(server_name, cfg, catalog_epoch)
        runtime_cfg, runtime_policy = _prepare_runtime_child_policy(
            cfg, self._fleet.config
        )
        try:
            async with contextlib.AsyncExitStack() as stack:
                session = await self._open_one_session(server_name, runtime_cfg, stack)

                return await self._invoke_oauth_tool(
                    session,
                    server_name,
                    original_name,
                    cfg,
                    arguments,
                    catalog_epoch,
                    runtime_policy,
                    approval_ref,
                )
        finally:
            if runtime_policy is not None:
                _close_runtime_child_policy(runtime_policy)

    async def _invoke_oauth_tool(
        self,
        session: _typing.Any,
        server_name: str,
        original_name: str,
        cfg: dict,
        arguments: dict[str, _typing.Any],
        catalog_epoch: int,
        runtime_policy: _typing.Any,
        approval_ref: str = "",
    ) -> MCPCallToolResult:
        """Run admission, schema reads and invocation on the caller's sole session."""

        def recheck_oauth_admission() -> None:
            self._assert_oauth_catalog(server_name, cfg, catalog_epoch)

        recheck_oauth_admission()
        descriptor = await self._assert_oauth_tool_admitted(
            server_name, original_name, session, runtime_policy
        )
        selected_disclosure = (
            self._schema_disclosure_for_tool(descriptor, original_name)
            if descriptor is not None
            else None
        )
        if original_name.startswith("operation_") and selected_disclosure is None:
            raise _fastmcp_exceptions.ToolError(
                "OpenAPI operation is not in the admitted child catalog"
            )

        from arbora.fleet.gateway_ops import check_native_transport

        def before_oauth_provider_call() -> None:
            recheck_oauth_admission()
            check_native_transport(server_name, original_name)

        if selected_disclosure is not None:
            manifest_model, manifest = selected_disclosure
            before_oauth_provider_call()
            await self._preflight_openapi_schema(
                session,
                manifest_model,
                manifest,
                descriptor,
                arguments,
                before_oauth_provider_call,
            )
        before_oauth_provider_call()
        # ARBORA-FLEET-R077: a plan-confirmed call arms this dedicated session
        # to accept the connector's confirmation, as the pooled path does.
        async with self._fleet.confirmations.armed(session, original_name):
            return await session.call_tool(
                original_name, arguments, **approval_call_kwargs(approval_ref)
            )
