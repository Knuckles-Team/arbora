"""Operator and role operations for the web interface (ARBORA-OPS-R913, R917).

Each operation here is a thin name over one served capability and keeps no
state. Three kinds exist:

* an engine method behind the pin-lag guard (``arbora.pin_lag.medulla``): a
  pinned engine that does not serve the method or request shape refuses
  ``UNAVAILABLE`` naming it, and a pin that serves it needs no change here;
* a name for another registered operation (pin toggles, the ingest pipeline,
  resources, commands), run through the invoke pipeline so the target's own
  scope, effect gate and audit apply to the caller;
* a connector call by ``fleet.call`` (workflows).

A secret in a fleet server configuration is a reference, never a value, and a
reply masks any value that is not one.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from typing import Any, Literal

from pydantic import Field, create_model

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send
from arbora.api.ops._nested import invoke_registered
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import Effect, OpSpec, Verb
from arbora.pin_lag.medulla import (
    PinLagUnavailable,
    refusal,
    require_methods,
    require_models,
)

_Name = (str, Field(min_length=1, max_length=256))
_OptName = (str | None, Field(default=None, min_length=1, max_length=256))
_Obj: tuple[Any, Any] = (dict[str, Any], Field(default_factory=dict))
_Limit = (int, Field(default=50, ge=1, le=200))
_Cursor = (str | None, Field(default=None, max_length=1024))

_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|private[_-]?key|credential)"
)
_REFERENCE = re.compile(r"^(env|openbao)://[A-Za-z0-9][A-Za-z0-9_./:-]{0,255}$")
_REDACTED = "<redacted>"
WORKFLOW_SERVER = "business-process"
HALF_LIFE_MS = 7 * 24 * 3600 * 1000


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def secrets_are_references(value: Any, path: str = "config") -> None:
    """Refuse a secret-named field whose value is not a reference; never echo it."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            where = f"{path}.{key}"
            if _SECRET_KEY.search(str(key)) and not _is_reference(child):
                raise OperationRefused(
                    "INVALID_ARGUMENT",
                    {
                        "field": where,
                        "reason": "a secret is an env:// or openbao:// reference",
                    },
                )
            secrets_are_references(child, where)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            secrets_are_references(child, f"{path}[{index}]")


def _is_reference(value: Any) -> bool:
    return isinstance(value, str) and _REFERENCE.fullmatch(value) is not None


def masked(value: Any) -> Any:
    """The value with every secret-named non-reference field replaced."""
    if isinstance(value, Mapping):
        return {
            key: (
                _REDACTED
                if _SECRET_KEY.search(str(key)) and not _is_reference(child)
                else masked(child)
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [masked(child) for child in value]
    return value


def _ms(moment: str, field_name: str) -> int:
    try:
        return int(
            datetime.fromisoformat(moment.replace("Z", "+00:00")).timestamp() * 1000
        )
    except ValueError:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"field": field_name, "reason": "an RFC 3339 instant"}
        ) from None


# --- engine-bound operations ----------------------------------------------

Build = Callable[[Any, Mapping[str, Any]], dict[str, Any]]
Project = Callable[[Any, Mapping[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Engine:
    """One engine method an operation is served by, and the shapes it must carry."""

    method: str
    build: Build
    project: Project
    models: tuple[str, ...] = ()
    idempotent: bool = False


def _tenant(context: Any) -> str:
    return str(context.caller.tenant)


def _registry_op(operation: str) -> Build:
    return lambda ctx, p: {
        "op": {"operation": operation, "name": p["name"], **_config_of(p)}
    }


def _config_of(params: Mapping[str, Any]) -> dict[str, Any]:
    config = params.get("config")
    if config is None:
        return {}
    secrets_are_references(config)
    return {"config": config}


def _value(answer: Any, _params: Mapping[str, Any]) -> dict[str, Any]:
    return {"value": masked(answer)}


def _page(answer: Any, key: str) -> dict[str, Any]:
    rows = answer.get(key, []) if isinstance(answer, Mapping) else []
    return {
        key: rows,
        "next_cursor": answer.get("next_cursor")
        if isinstance(answer, Mapping)
        else None,
    }


def _calendar(answer: Any, _params: Mapping[str, Any]) -> dict[str, Any]:
    source = answer if isinstance(answer, Mapping) else {}
    return {"entries": source.get("entries", []), "logs": source.get("logs", [])}


def _code(answer: Any, _params: Mapping[str, Any]) -> dict[str, Any]:
    return {"items": answer.get("items", []) if isinstance(answer, Mapping) else []}


def _maintain(_ctx: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "delete": bool(params.get("delete", False)),
        "evict_threshold": float(params.get("evict_threshold", 0.0)),
        "half_life_ms": int(params.get("half_life_ms", HALF_LIFE_MS)),
        "ids": list(params.get("ids", [])),
        "now_ms": _now_ms(),
    }


def _template_get(answer: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    entry = answer.get("entry", answer) if isinstance(answer, Mapping) else None
    if not isinstance(entry, Mapping) or not entry.get("template_id"):
        raise OperationRefused("NOT_FOUND")
    return {"value": dict(entry)}


def _instantiate(ctx: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    binding = dict(params["binding"])
    instance = params.get("instance_id") or binding.pop("instance_id", None)
    if not isinstance(instance, str) or not instance:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "instance_id"})
    request: dict[str, Any] = {
        "agent_id": instance,
        "template_id": params["template_id"],
        "tenant_id": _tenant(ctx),
        "bindings": binding.pop("bindings", None),
    }
    if binding.get("entry_revision") is not None:
        request["entry_revision"] = binding["entry_revision"]
    return {"op": {"op": "instantiate", "request": request}}


_ENGINE: dict[str, Engine] = {
    "fleet.servers.register": Engine(
        "FleetServerRegistry", _registry_op("register"), _value, idempotent=True
    ),
    "fleet.servers.update": Engine(
        "FleetServerRegistry", _registry_op("update"), _value, idempotent=True
    ),
    "fleet.servers.remove": Engine(
        "FleetServerRegistry", _registry_op("remove"), _value, idempotent=True
    ),
    "fleet.servers.config.get": Engine(
        "FleetServerRegistry", _registry_op("get_config"), _value
    ),
    "schedule.tasks.read": Engine(
        "ScheduleCalendar",
        lambda ctx, p: {
            "from_ms": _ms(p["from_"] if "from_" in p else p["from"], "from"),
            "to_ms": _ms(p["to"], "to"),
        },
        _calendar,
    ),
    "ops.maintenance.status": Engine("MaintainStatus", lambda ctx, p: {}, _value),
    "ops.maintenance.run": Engine(
        "Maintain", lambda ctx, p: _maintain(ctx, p), _value, idempotent=True
    ),
    "code.context.read": Engine(
        "CodeContext",
        lambda ctx, p: {
            "action": p["action"],
            **({"symbol": p["symbol"]} if p.get("symbol") else {}),
        },
        _code,
    ),
    "roles.templates.list": Engine(
        "AgentTemplate",
        lambda ctx, p: {
            "op": {
                "op": "list",
                "tenant_id": _tenant(ctx),
                "limit": p["limit"],
                **({"cursor": p["cursor"]} if p.get("cursor") else {}),
            }
        },
        lambda answer, p: _page(answer, "templates"),
        models=("AgentTemplateOpList",),
    ),
    "roles.templates.get": Engine(
        "AgentTemplate",
        lambda ctx, p: {
            "op": {
                "op": "current",
                "template_id": p["template_id"],
                "tenant_id": _tenant(ctx),
            }
        },
        _template_get,
    ),
}

#: Engine methods a role binding needs before it may bind a template (R917.2).
PROFILE_METHOD = "BusinessProfile"


async def _send(context: Any, spec: Engine, params: Mapping[str, Any]) -> Any:
    try:
        require_methods(spec.method)
        require_models(spec.models)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    request = spec.build(context, params)
    return await engine_send(context, spec.method, request, idempotent=spec.idempotent)


async def handle_engine(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve one operation over its engine method, refusing a missing capability."""
    spec = _ENGINE[op.id]
    return spec.project(await _send(context, spec, params), params)


async def _profile_set(context: Any) -> None:
    try:
        require_methods(PROFILE_METHOD)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    answer = await engine_send(
        context, PROFILE_METHOD, {"op": {"op": "get", "tenant_id": _tenant(context)}}
    )
    profile = answer.get("profile") if isinstance(answer, Mapping) else None
    if not profile:
        raise OperationRefused("PROFILE_UNSET")


async def handle_instance_bind(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Bind one template to the tenant profile; the engine refuses a widening binding."""
    await _profile_set(context)
    try:
        require_methods("AgentTemplate")
    except PinLagUnavailable as error:
        raise refusal(error) from error
    request = _instantiate(context, params)
    answer = await engine_send(context, "AgentTemplate", request, idempotent=True)
    entry = answer.get("entry", answer) if isinstance(answer, Mapping) else {}
    return {"value": dict(entry)}


# --- names for other registered operations ---------------------------------


@dataclass(frozen=True, slots=True)
class Alias:
    """An operation that is a name for another, run under the target's own scope."""

    target: Callable[[Mapping[str, Any]], str]
    arguments: Callable[[Mapping[str, Any]], dict[str, Any]]


def _toggle_target(params: Mapping[str, Any]) -> str:
    return "fleet.tools.pin" if params["enabled"] else "fleet.tools.unpin"


def _submit(params: Mapping[str, Any]) -> dict[str, Any]:
    if not params.get("source"):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "source"})
    return {"kind": "ingest_url", "target": params["source"], "options": {}}


_ALIAS: dict[str, Alias] = {
    "fleet.skills.toggle": Alias(_toggle_target, lambda p: {"items": [p["id"]]}),
    "fleet.tools.toggle": Alias(_toggle_target, lambda p: {"items": [p["id"]]}),
    "ingest.pipeline.status": Alias(lambda p: "ingest.jobs.list", lambda p: {}),
    "ingest.pipeline.run": Alias(lambda p: "ingest.submissions.create", _submit),
    "roles.instances.retire": Alias(
        lambda p: "agents.library.delete", lambda p: {"id": p["instance_id"]}
    ),
    "resources.list": Alias(lambda p: "capacity.throttle.status", lambda p: {}),
    "resources.spawn": Alias(
        lambda p: "agents.run",
        lambda p: {"message": {"kind": p["kind"], **p["config"]}},
    ),
}


async def handle_alias(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    alias = _ALIAS[op.id]
    value = await invoke_registered(
        context, alias.target(params), alias.arguments(params)
    )
    return {"value": value}


# --- commands: an operation id is the only command --------------------------


def _served(context: Any) -> list[OpSpec]:
    services = getattr(context, "operation_services", None)
    if services is None:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the operation registry is not composed"}
        )
    held = context.caller.effective_scopes
    return [
        op
        for op in services.registry
        if op.scopes <= held and not op.id.startswith("commands.")
    ]


async def handle_commands_autocomplete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    prefix = str(params.get("prefix", ""))
    ids = sorted(
        served.id for served in _served(context) if served.id.startswith(prefix)
    )
    return {"value": {"commands": ids[:200]}}


async def handle_commands_execute(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    result = await invoke_registered(
        context,
        str(params["command"]),
        params["args"],
        only=lambda target: not target.id.startswith("commands."),
    )
    return {"value": {"result": result}}


# --- workflows: the business-process connector through the fleet -------------

_WORKFLOW_TOOL = {
    "workflows.list": "list",
    "workflows.create": "create",
    "workflows.run": "run",
}


async def handle_workflow(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    arguments = {k: v for k, v in params.items() if v is not None}
    reply = await invoke_registered(
        context,
        "fleet.call",
        {
            "server": WORKFLOW_SERVER,
            "tool": _WORKFLOW_TOOL[op.id],
            "arguments": arguments,
        },
    )
    value = reply.get("value", reply) if isinstance(reply, Mapping) else reply
    if op.id != "workflows.run":
        return {"value": value}
    run_id = value.get("run_id") if isinstance(value, Mapping) else None
    if not isinstance(run_id, str) or not run_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the connector returned no run id"}
        )
    return {"value": {"run_id": run_id}}


# --- registration -------------------------------------------------------------

#: op id -> (summary, verb, effect, scope, params fields, handler)
_TABLE: dict[str, tuple[str, Verb, Effect, str, dict[str, Any], str]] = {
    "fleet.servers.register": (
        "Register one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name, "config": _Obj},
        "handle_engine",
    ),
    "fleet.servers.update": (
        "Update one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name, "config": _Obj},
        "handle_engine",
    ),
    "fleet.servers.remove": (
        "Remove one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name},
        "handle_engine",
    ),
    "fleet.servers.config.get": (
        "Read one fleet server configuration with secrets as references",
        Verb.ASK,
        Effect.READ,
        "fleet:read",
        {"name": _Name},
        "handle_engine",
    ),
    "fleet.skills.toggle": (
        "Enable or disable one skill",
        Verb.WRITE,
        Effect.WRITE,
        "fleet:control",
        {"id": _Name, "enabled": (bool, ...)},
        "handle_alias",
    ),
    "fleet.tools.toggle": (
        "Enable or disable one tool",
        Verb.WRITE,
        Effect.WRITE,
        "fleet:control",
        {"id": _Name, "enabled": (bool, ...)},
        "handle_alias",
    ),
    "schedule.tasks.read": (
        "Read the schedule calendar and logs",
        Verb.ASK,
        Effect.READ,
        "ops:read",
        {
            "from_": (str, Field(alias="from", min_length=1, max_length=64)),
            "to": (str, Field(min_length=1, max_length=64)),
        },
        "handle_engine",
    ),
    "ops.maintenance.status": (
        "Read the maintenance status",
        Verb.ASK,
        Effect.READ,
        "ops:read",
        {},
        "handle_engine",
    ),
    "ops.maintenance.run": (
        "Run one maintenance cycle",
        Verb.WRITE,
        Effect.ADMIN,
        "ops:admin",
        {
            "ids": (list[str], Field(default_factory=list, max_length=1000)),
            "delete": (bool, False),
            "evict_threshold": (float, Field(default=0.0, ge=0.0, le=1.0)),
            "half_life_ms": (int, Field(default=HALF_LIFE_MS, ge=1)),
        },
        "handle_engine",
    ),
    "ingest.pipeline.status": (
        "Read the ingest pipeline status",
        Verb.ASK,
        Effect.READ,
        "ingest:read",
        {},
        "handle_alias",
    ),
    "ingest.pipeline.run": (
        "Run the ingest pipeline",
        Verb.WRITE,
        Effect.WRITE,
        "ingest:write",
        {"source": _OptName},
        "handle_alias",
    ),
    "workflows.list": (
        "List workflows",
        Verb.FIND,
        Effect.READ,
        "work:read",
        {"limit": _Limit, "cursor": _Cursor},
        "handle_workflow",
    ),
    "workflows.create": (
        "Create a workflow",
        Verb.WRITE,
        Effect.WRITE,
        "work:write",
        {"definition": _Obj},
        "handle_workflow",
    ),
    "workflows.run": (
        "Run a workflow",
        Verb.WRITE,
        Effect.WRITE,
        "work:write",
        {"workflow_id": _Name, "inputs": _Obj},
        "handle_workflow",
    ),
    "code.context.read": (
        "Read code navigation context",
        Verb.ASK,
        Effect.READ,
        "node:read",
        {"action": (Literal["nav", "instances"], ...), "symbol": _OptName},
        "handle_engine",
    ),
    "commands.execute": (
        "Execute a command that is a served operation",
        Verb.WRITE,
        Effect.WRITE,
        "registry:read",
        {"command": _Name, "args": _Obj},
        "handle_commands_execute",
    ),
    "commands.autocomplete": (
        "Complete a command from the served operations",
        Verb.FIND,
        Effect.READ,
        "registry:read",
        {"prefix": (str, Field(default="", max_length=256))},
        "handle_commands_autocomplete",
    ),
    "resources.list": (
        "List resources",
        Verb.FIND,
        Effect.READ,
        "capacity:read",
        {},
        "handle_alias",
    ),
    "resources.spawn": (
        "Spawn a resource",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"kind": _Name, "config": _Obj},
        "handle_alias",
    ),
    "roles.templates.list": (
        "List role templates",
        Verb.FIND,
        Effect.READ,
        "registry:read",
        {"limit": _Limit, "cursor": _Cursor},
        "handle_engine",
    ),
    "roles.templates.get": (
        "Read one role template",
        Verb.ASK,
        Effect.READ,
        "registry:read",
        {"template_id": _Name},
        "handle_engine",
    ),
    "roles.instances.create": (
        "Bind a role template to the tenant profile",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"template_id": _Name, "binding": _Obj},
        "handle_instance_bind",
    ),
    "roles.instances.update": (
        "Narrow a role instance binding",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"instance_id": _Name, "template_id": _Name, "binding": _Obj},
        "handle_instance_bind",
    ),
    "roles.instances.retire": (
        "Retire a role instance",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"instance_id": _Name},
        "handle_alias",
    ),
}


def _replay_owner(op_id: str) -> str:
    if op_id in _ENGINE:
        return f"medulla:{_ENGINE[op_id].method}"
    if op_id in _ALIAS:
        return "nested:registered-operation"
    if op_id in _WORKFLOW_TOOL:
        return "fleet:call"
    if op_id == "commands.execute":
        return "nested:registered-operation"
    return "medulla:AgentTemplate"


#: Each write replays under the owner that holds its effect, never in this module.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    op_id: _replay_owner(op_id)
    for op_id, row in _TABLE.items()
    if row[2] is not Effect.READ and row[2] is not Effect.ADMIN
}


def table_ids() -> tuple[str, ...]:
    return tuple(_TABLE)


@cache
def operations() -> tuple[OpSpec, ...]:
    specs: list[OpSpec] = []
    for op_id, (summary, verb, effect, scope, fields, handler) in _TABLE.items():
        model = create_model(
            "Params_" + op_id.replace(".", "_"), __base__=Params, **fields
        )
        common: dict[str, Any] = {
            "op_id": op_id,
            "summary": summary,
            "examples": (summary.lower(),),
            "params": model,
            "result": EngineReply,
            "handler": f"arbora.api.ops.operator_surface.{handler}",
            "scope": scope,
        }
        if effect is Effect.READ:
            specs.append(build_read_op(verb=verb, **common))
        else:
            specs.append(build_write_op(verb=verb, effect=effect, **common))
    return tuple(specs)


specs = operations
