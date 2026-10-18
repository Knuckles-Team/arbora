"""Compose the operation-invoke API that the served ``arbora`` process mounts.

The served process exposes ``POST <prefix>/v1/ops/<op_id>`` and the hosted
A2A operation bridge through one :class:`HostedInvocation`. This module joins
the parts that already exist:

* the generated operation registry (``registry_factory.get_registry``);
* the caller and service EG client factories and the bound operation runtime
  (``arbora.api.invoke.bootstrap``) over one :class:`EpistemicClientPool`;
* the EG control-lease plan store;
* the policy gate in the configured Eunomia mode;
* a provider schema validator over the EG contract schemas;
* the fleet-call effect of the governed fleet gateway, when the served root
  supplies one.

The process authority is the session the serving lifecycle mints. It is read
at call time, so a renewed process credential applies without a rebuild.
"""

from __future__ import annotations

import importlib.resources
import json
from collections.abc import Callable, Mapping
from functools import cache
from pathlib import Path
from typing import Any

from arbora.api.registry import EgSchemaRef

__all__ = [
    "PoolLeases",
    "refused_fleet_effect",
    "compose_served_invocation",
    "engine_endpoint",
    "served_invocation_from_settings",
    "validate_provider_schema",
]

#: The lease kind of console plans in the EG control-lease allowlist.
PLAN_LEASE_KIND = "arbora.plan"


@cache
def _contract_document(filename: str) -> dict[str, Any]:
    root = Path(str(importlib.resources.files("epistemic_graph") / "contract"))
    relative = Path(filename.removeprefix("contract/"))
    if ".." in relative.parts:
        raise ValueError("invalid EG schema reference")
    document = json.loads((root / relative).read_bytes())
    if not isinstance(document, dict):
        raise ValueError("EG contract schema document is not an object")
    return document


def validate_provider_schema(
    model: EgSchemaRef, params: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Validate ``params`` against the EG contract schema ``model`` names.

    Raises :class:`ValueError` for a non-conforming input; the invoke pipeline
    maps that to ``INVALID_ARGUMENT``.
    """
    from jsonschema import Draft202012Validator, ValidationError
    from referencing import Registry, Resource
    from referencing.jsonschema import DRAFT202012

    filename, separator, pointer = model.path.partition("#")
    if not separator or not filename.startswith("contract/schemas/"):
        raise ValueError("invalid EG schema reference")
    document = _contract_document(filename)
    if _is_request_envelope(document, pointer):
        # A generated method binding names the whole wire request; the
        # operation input is its ``params`` member.
        pointer = f"{pointer}/properties/params"
    elif _is_parameterless_envelope(document, pointer):
        # A method without ``params`` (``NodeCount``) takes an empty input;
        # its envelope requires only ``method``, which no caller sends.
        if params:
            raise ValueError("operation input does not match its EG schema")
        return {}
    uri = f"urn:arbora:eg-contract:{filename}"
    resource = Resource.from_contents(document, default_specification=DRAFT202012)
    validator = Draft202012Validator(
        {"$ref": f"{uri}#{pointer}"},
        registry=Registry().with_resource(uri, resource),
    )
    try:
        validator.validate(dict(params))
    except ValidationError as exc:
        raise ValueError("operation input does not match its EG schema") from exc
    return dict(params)


def _selected(document: Mapping[str, Any], pointer: str) -> Any:
    selected: Any = document
    for segment in pointer.lstrip("/").split("/"):
        if not isinstance(selected, Mapping) or segment not in selected:
            return None
        selected = selected[segment]
    return selected


def _is_parameterless_envelope(document: Mapping[str, Any], pointer: str) -> bool:
    selected = _selected(document, pointer)
    if not isinstance(selected, Mapping):
        return False
    properties = selected.get("properties")
    return (
        selected.get("required") == ["method"]
        and isinstance(properties, Mapping)
        and set(properties) == {"method"}
    )


def _is_request_envelope(document: Mapping[str, Any], pointer: str) -> bool:
    selected = _selected(document, pointer)
    required = selected.get("required") if isinstance(selected, Mapping) else None
    properties = selected.get("properties") if isinstance(selected, Mapping) else None
    return (
        isinstance(required, list)
        and {"method", "params"} <= set(required)
        and isinstance(properties, Mapping)
        and "params" in properties
    )


class PoolLeases:
    """``control_leases`` of the service-authorized EG client, resolved per call."""

    def __init__(self, service_client: Callable[[], Any]) -> None:
        self._service_client = service_client

    def __getattr__(self, name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> Any:
            client = await self._service_client()
            return await getattr(client.control_leases, name)(*args, **kwargs)

        return call


async def refused_fleet_effect(
    _op: Any, params: Mapping[str, Any], _caller: Any
) -> Any:
    """Refuse ``fleet.call`` until the served root composes the governed fleet ops.

    The governed gateway (``arbora.fleet.gateway_ops.compose_fleet_gateway``)
    needs the multiplexer operation service, which the served root does not
    compose yet; ``fleet.call`` fails closed instead of calling ungoverned.
    """
    from arbora.api.invoke.pipeline import OperationRefused

    del params
    raise OperationRefused(
        "UNAVAILABLE", {"reason": "governed fleet calls unavailable"}
    )


def engine_endpoint(endpoints: Any) -> dict[str, str]:
    """``{"socket_path": ...}`` or ``{"tcp_addr": ...}`` for the first endpoint."""
    values = [str(item).strip() for item in (endpoints or []) if str(item).strip()]
    if not values:
        raise ValueError("GRAPH_SERVICE_ENDPOINTS names no engine endpoint")
    endpoint = values[0]
    if endpoint.startswith("unix://"):
        return {"socket_path": endpoint.removeprefix("unix://")}
    if endpoint.startswith("/"):
        return {"socket_path": endpoint}
    return {"tcp_addr": endpoint.removeprefix("tcp://")}


def compose_served_invocation(
    *,
    pool: Any,
    graph: str,
    process_session: Callable[[], Any],
    policy_mode: str = "none",
    plan_seal_key: bytes | None = None,
    bindings: Mapping[str, Any] | None = None,
    fleet_effect: Any = None,
) -> Any:
    """One admitted :class:`HostedInvocation` over the served EG pool."""
    from arbora.api.http.auth import (
        AmbientHTTPAuthenticator,
        verified_bearer_session_for_request,
    )
    from arbora.api.invoke.bootstrap import (
        build_invoke_services,
        build_operation_runtime,
        build_service_claims,
    )
    from arbora.api.invoke.plan import EgPlanStore
    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.policy.eunomia import PolicyGate
    from arbora.epistemic import ClientContext
    from arbora.mcp_server.hosted_invocation import HostedInvocation

    def session() -> Any:
        current = process_session()
        if current is None:
            raise RuntimeError("the process graph authority is not minted")
        return current

    def connect_context(tenant: str) -> Any:
        current = session()
        return ClientContext.from_actor(
            current.actor,
            tenant=tenant,
            audience=current.audience,
            scopes=current.scopes,
            policy_version=current.policy_version,
        )

    minted = session()

    async def service_client() -> Any:
        return await pool.connected_client(connect_context(session().tenant), graph)

    runtime = build_operation_runtime(
        pool=pool,
        graph=graph,
        connect_context=connect_context,
        service_scopes=frozenset(minted.scopes),
        service_principal=str(minted.actor.actor_id),
        service_claims=build_service_claims(
            lambda: session().actor,
            audience=str(minted.audience),
            policy_version=str(minted.policy_version),
        ),
        bindings=bindings,
    )

    async def audit_client(tenant: str) -> Any:
        return await pool.connected_client(connect_context(tenant), graph)

    registry = get_registry()
    changes: dict[str, Any] = {"schema_validate": validate_provider_schema}
    if registry.get("fleet.call") is not None:
        if fleet_effect is None:
            raise ValueError("fleet.call needs the governed fleet gateway effect")
        from arbora.mcp_server.effect_journal import EgEffectJournal

        changes["fleet_effect"] = fleet_effect
        # A governed child call is reserved and recorded in EG before dispatch.
        from arbora.deployment.semantic_provisioning import CONTROL_GRAPH

        async def control_client() -> Any:
            # EG holds WorkItems on the tenant's control graph.
            return await pool.connected_client(
                connect_context(session().tenant), CONTROL_GRAPH
            )

        changes["effects"] = EgEffectJournal(
            control_client,
            claims=lambda: connect_context(session().tenant).to_claims(),
        )
        changes["external_effect_operations"] = frozenset({"fleet.call"})
    services = build_invoke_services(
        registry=registry,
        runtime=runtime,
        plans=_LeaseClient(PoolLeases(service_client)).store(
            EgPlanStore, lease_kind=PLAN_LEASE_KIND, seal_key=plan_seal_key
        ),
        policy_gate=PolicyGate(policy_mode),
        audit_client_factory=audit_client,
        **changes,
    )
    return HostedInvocation(
        services=services,
        authenticator=AmbientHTTPAuthenticator(
            session_for_request=verified_bearer_session_for_request
        ),
    )


class _LeaseClient:
    """The ``client`` shape ``EgPlanStore`` reads: one ``control_leases``."""

    def __init__(self, leases: PoolLeases) -> None:
        self.control_leases = leases

    def store(self, factory: Any, **kwargs: Any) -> Any:
        return factory(self, **kwargs)


def served_invocation_from_settings(
    *,
    process_session: Callable[[], Any],
    bindings: Mapping[str, Any],
    fleet_effect: Any,
) -> Any:
    """The served invocation from the deployment settings, or ``None``.

    Without ``GRAPH_SERVICE_ENDPOINTS`` the process has no engine to bind and
    serves no operation-invoke API.
    """
    from arbora.core.settings import runtime_config, setting
    from arbora.mcp_server.served_engine import process_engine_pool

    if not runtime_config.graph_service_endpoints:
        return None
    # The one process pool: startup composition reads EG through it too.
    pool = process_engine_pool()
    raw_key = str(setting("ARBORA_PLAN_SEAL_KEY_HEX", "") or "").strip()
    return compose_served_invocation(
        pool=pool,
        # Callers are admitted only on the process authority's own graph (the
        # tenant graph minted with the session) unless a deployment pins one.
        graph=str(setting("ARBORA_GRAPH", "") or "").strip()
        or str(process_session().graph),
        process_session=process_session,
        policy_mode=str(setting("EUNOMIA_MODE", "none") or "none"),
        plan_seal_key=bytes.fromhex(raw_key) if raw_key else None,
        bindings=bindings,
        fleet_effect=fleet_effect,
    )
