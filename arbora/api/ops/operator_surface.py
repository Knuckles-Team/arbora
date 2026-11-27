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

import inspect
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
from arbora.api.ops.engine_calls import engine_send
from arbora.api.ops.knowledge import EngineReply
from arbora.api.ops.nested_invoke import invoke_registered
from arbora.api.ops.pin_guards import refusal
from arbora.api.registry import Effect, OpSpec, Verb
from arbora.pin_lag.medulla import PinLagUnavailable, require_methods, require_models

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

Build = Callable[[Any, Mapping[str, Any]], Any]
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


async def _calendar_request(ctx: Any, p: Mapping[str, Any]) -> dict[str, Any]:
    """``AnalyticsJob`` ``ScheduleCalendar`` over the caller's graph (at most 31 days)."""
    from arbora.api.invoke.bootstrap import caller_dispatch_graph

    return {
        "op": {
            "ScheduleCalendar": {
                "graph": await caller_dispatch_graph(ctx),
                "from_ms": _ms(p["from_"] if "from_" in p else p["from"], "from"),
                "to_ms": _ms(p["to"], "to"),
            }
        }
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


def _enabled_override(ctx: Any, p: Mapping[str, Any]) -> dict[str, Any]:
    """``FleetCatalog`` ``set_override`` of the durable ``enabled`` flag of one component."""
    return {
        "op": {
            "op": "set_override",
            "request": {
                "component_id": p["id"],
                "value": {"field": "enabled", "enabled": bool(p["enabled"])},
            },
        }
    }


_ENGINE: dict[str, Engine] = {
    # Skill and tool toggles are the engine's operator override (hard cutover
    # from the session pins), refused UNAVAILABLE until the pinned engine serves it.
    "fleet.skills.toggle": Engine(
        "FleetCatalog",
        _enabled_override,
        _value,
        models=("FleetOverrideEnabled",),
        idempotent=True,
    ),
    "fleet.tools.toggle": Engine(
        "FleetCatalog",
        _enabled_override,
        _value,
        models=("FleetOverrideEnabled",),
        idempotent=True,
    ),
    "schedule.tasks.read": Engine(
        "AnalyticsJob",
        _calendar_request,
        _calendar,
        models=("JobOpScheduleCalendar",),
    ),
    "ops.maintenance.run": Engine(
        "Maintain", lambda ctx, p: _maintain(ctx, p), _value, idempotent=True
    ),
    "code.context.read": Engine(
        "CodeNavigation",
        lambda ctx, p: {
            "request": {
                "action": p["action"],
                **({"symbol": p["symbol"]} if p.get("symbol") else {}),
                **({"limit": p["limit"]} if p.get("limit") else {}),
            }
        },
        _code,
    ),
    "roles.templates.list": Engine(
        "AgentTemplate",
        lambda ctx, p: {
            "op": {
                "op": "list",
                "request": {
                    "tenant_id": _tenant(ctx),
                    "limit": p["limit"],
                    **({"cursor": p["cursor"]} if p.get("cursor") else {}),
                },
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
PROFILE_METHOD = "KvGet"
#: The tenant business profile is one tenant-shared KV record (R917).
PROFILE_NAMESPACE = "business.profile"
PROFILE_KEY = "profile"
PROFILE_FIELDS = frozenset(
    {
        "entity",
        "brand",
        "products",
        "policies",
        "owners",
        "approval_matrix",
        "claim_rules",
    }
)
#: The maintenance cycle runs as one engine schedule of this name.
MAINTENANCE_SCHEDULE = "maintenance"
SERVER_TTL_SECS = 3600
_ACTIVE_STATUS = frozenset({"submitted", "ready", "leased", "running"})
_SERVER_OWN = frozenset({"url", "transport", "ttl_secs"})
_WORK_ITEM_KIND = "workflow.run"
_MAX_TENANT_IN_FLIGHT = 16


async def _send(context: Any, spec: Engine, params: Mapping[str, Any]) -> Any:
    try:
        require_methods(spec.method)
        require_models(spec.models)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    request = spec.build(context, params)
    if inspect.isawaitable(request):
        request = await request
    return await engine_send(context, spec.method, request, idempotent=spec.idempotent)


async def handle_engine(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Serve one operation over its engine method, refusing a missing capability."""
    spec = _ENGINE[op.id]
    return spec.project(await _send(context, spec, params), params)


def _entry_value(entry: Any) -> Any:
    """The decoded JSON a KV entry holds, or ``None`` for an absent key."""
    import json

    if not isinstance(entry, Mapping) or "value" not in entry:
        return None
    raw = entry["value"]
    if isinstance(raw, (list, tuple)):
        raw = bytes(raw)
    if isinstance(raw, (bytes, bytearray, memoryview)):
        try:
            return json.loads(bytes(raw))
        except ValueError:
            return None
    return raw


async def read_profile(context: Any) -> tuple[dict[str, Any], int]:
    """The tenant business profile and its KV version; ``PROFILE_UNSET`` when absent."""
    try:
        require_methods(PROFILE_METHOD)
    except PinLagUnavailable as error:
        raise refusal(error) from error
    entry = await engine_send(
        context,
        PROFILE_METHOD,
        {"namespace": PROFILE_NAMESPACE, "key": PROFILE_KEY, "shared": True},
    )
    profile = _entry_value(entry)
    if not isinstance(profile, dict) or not profile:
        raise OperationRefused("PROFILE_UNSET")
    return profile, int(entry.get("version", 0))


async def _profile_set(context: Any) -> int:
    return (await read_profile(context))[1]


async def write_profile(
    context: Any, profile: Mapping[str, Any], expected_version: int
) -> int:
    """Compare-and-set the profile on its KV version (``0`` means it must not exist)."""
    import json

    unknown = sorted(set(profile) - PROFILE_FIELDS)
    if unknown:
        raise OperationRefused("INVALID_ARGUMENT", {"field": unknown[0]})
    try:
        require_methods("KvCas")
    except PinLagUnavailable as error:
        raise refusal(error) from error
    answer = await engine_send(
        context,
        "KvCas",
        {
            "namespace": PROFILE_NAMESPACE,
            "key": PROFILE_KEY,
            "shared": True,
            "expected_version": expected_version,
            "new": json.dumps(dict(profile), sort_keys=True).encode(),
        },
        idempotent=True,
    )
    if not (isinstance(answer, Mapping) and answer.get("swapped")):
        raise OperationRefused(
            "REVISION_CONFLICT", {"expected_version": expected_version}
        )
    return int(answer.get("version", 0))


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


# --- fleet server registry: RegisterServer and ListRegisteredServers -------------


def _server_request(
    name: str, config: Mapping[str, Any], desired: str
) -> dict[str, Any]:
    import json

    secrets_are_references(config)
    url = config.get("url")
    if not isinstance(url, str) or not url:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "config.url"})
    request: dict[str, Any] = {
        "name": name,
        "url": url,
        "desired": desired,
        "ttl_secs": int(config.get("ttl_secs", SERVER_TTL_SECS)),
        # References only: the engine stores them and never resolves them.
        "resources_json": json.dumps(
            {k: v for k, v in config.items() if k not in _SERVER_OWN}, sort_keys=True
        ),
    }
    if config.get("transport"):
        request["transport"] = config["transport"]
    return request


async def _registered_server(context: Any, name: str) -> dict[str, Any]:
    """The registry row of ``name``; an absent row answers exactly like a foreign one."""
    cursor: Any = None
    for _page in range(64):
        request: dict[str, Any] = {"limit": 256}
        if cursor is not None:
            request["cursor"] = cursor
        page = await engine_send(context, "ListRegisteredServers", {"request": request})
        for row in page.get("entries", []) if isinstance(page, Mapping) else []:
            if isinstance(row, Mapping) and row.get("name") == name:
                return dict(row)
        cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
        if cursor is None:
            break
    raise OperationRefused("NOT_FOUND")


async def handle_server(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Register, update, remove or read one fleet server over the engine registry."""
    import json

    try:
        require_methods("RegisterServer", "ListRegisteredServers")
    except PinLagUnavailable as error:
        raise refusal(error) from error
    name = str(params["name"])
    if op.id == "fleet.servers.config.get":
        return {"value": masked(await _registered_server(context, name))}
    if op.id == "fleet.servers.remove":
        # Remove is disabled now; the engine sweep drops the row at lease expiry.
        row = await _registered_server(context, name)
        request = {
            "name": name,
            "url": row["url"],
            "desired": "disabled",
            "ttl_secs": int(row.get("ttl_secs", SERVER_TTL_SECS)),
            "resources_json": json.dumps(row.get("resources") or {}, sort_keys=True),
        }
        if row.get("transport"):
            request["transport"] = row["transport"]
    else:
        request = _server_request(name, params["config"], "enabled")
    await engine_send(context, "RegisterServer", request, idempotent=True)
    return {"value": {"name": name, "desired": request["desired"]}}


# --- maintenance status: schedule list, schedule ticks and work items -------------


async def handle_maintenance_status(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Compose the maintenance schedule state; ``Maintain`` is never called here."""
    from arbora.api.invoke.bootstrap import caller_dispatch_graph

    try:
        require_methods("AnalyticsJob", "ListWorkItems")
        require_models(("JobOpScheduleList", "JobOpScheduleTicks"))
    except PinLagUnavailable as error:
        raise refusal(error) from error
    graph = await caller_dispatch_graph(context)
    listed = await engine_send(
        context, "AnalyticsJob", {"op": {"ScheduleList": {"graph": graph}}}
    )
    schedule = next(
        (
            row
            for row in (
                listed.get("schedules", []) if isinstance(listed, Mapping) else []
            )
            if isinstance(row, Mapping) and row.get("name") == MAINTENANCE_SCHEDULE
        ),
        None,
    )
    if schedule is None:
        return {"value": {"state": "unscheduled", "pending": 0, "last_tick": None}}
    ticks = await engine_send(
        context,
        "AnalyticsJob",
        {
            "op": {
                "ScheduleTicks": {
                    "graph": graph,
                    "name": MAINTENANCE_SCHEDULE,
                    "limit": 1,
                }
            }
        },
    )
    last = (ticks.get("ticks") or [None])[-1] if isinstance(ticks, Mapping) else None
    items = await engine_send(
        context,
        "ListWorkItems",
        {"tenant": _tenant(context), "kind": schedule.get("task_kind"), "limit": 200},
    )
    pending = sum(
        1
        for item in (items.get("items", []) if isinstance(items, Mapping) else [])
        if isinstance(item, Mapping) and item.get("status") in _ACTIVE_STATUS
    )
    return {
        "value": {"state": schedule.get("state"), "pending": pending, "last_tick": last}
    }


# --- names for other registered operations ---------------------------------


@dataclass(frozen=True, slots=True)
class Alias:
    """An operation that is a name for another, run under the target's own scope."""

    target: Callable[[Mapping[str, Any]], str]
    arguments: Callable[[Mapping[str, Any]], dict[str, Any]]


def _submit(params: Mapping[str, Any]) -> dict[str, Any]:
    if not params.get("source"):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "source"})
    return {"kind": "ingest_url", "target": params["source"], "options": {}}


_ALIAS: dict[str, Alias] = {
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

_WORKFLOW_TOOL = {"workflows.list": "list", "workflows.create": "create"}


async def _submit_run(context: Any, params: Mapping[str, Any]) -> str:
    """Submit one workflow run as an engine WorkItem; its id is the run id."""
    import hashlib
    import json

    from arbora.api.domain.mcp_server.effect_journal import request_context
    from arbora.api.invoke.bootstrap import caller_dispatch_graph
    from arbora.core.claims import claim_values

    try:
        require_methods("SubmitWorkItem")
    except PinLagUnavailable as error:
        raise refusal(error) from error
    key = context.idempotency_key
    if not key:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "idempotency_key"})
    graph = await caller_dispatch_graph(context)
    claims = claim_values(context.caller.engine_claims)

    class _Graph:
        graph_name = graph

    body = {"workflow_id": params["workflow_id"], "inputs": dict(params["inputs"])}
    digest = hashlib.sha256(
        json.dumps([_tenant(context), body], sort_keys=True).encode()
    ).hexdigest()
    answer = await engine_send(
        context,
        "SubmitWorkItem",
        {
            "request": {
                "schema_version": "1",
                "context": request_context(claims, _Graph(), key),
                "idempotency_key": key,
                "command_digest": digest,
                "kind": _WORK_ITEM_KIND,
                "priority": 0,
                "depends_on": [],
                "input_ref": f"workflow:{params['workflow_id']}",
                "policy_digest": hashlib.sha256(
                    str(claims.get("policy_version") or "none").encode()
                ).hexdigest(),
                "catalog_digest": digest,
                "model_digest": "none",
                "max_attempts": 1,
                "max_tenant_in_flight": _MAX_TENANT_IN_FLIGHT,
                "metadata": body,
                "provenance_refs": [str(context.caller.principal)],
            }
        },
        idempotent=True,
    )
    run_id = (
        answer.get("work_item_id") or answer.get("id")
        if isinstance(answer, Mapping)
        else None
    )
    if not isinstance(run_id, str) or not run_id:
        raise OperationRefused(
            "UNAVAILABLE", {"reason": "the engine returned no work item id"}
        )
    return run_id


async def handle_workflow(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    if op.id == "workflows.run":
        run_id = await _submit_run(context, params)
        starter = context.services.get("workflow_worker")
        if starter is not None:
            starter.start(run_id, context.caller.session)
        return {"value": {"run_id": run_id}}
    reply = await invoke_registered(
        context,
        "fleet.call",
        {
            "server": WORKFLOW_SERVER,
            "tool": _WORKFLOW_TOOL[op.id],
            "arguments": {k: v for k, v in params.items() if v is not None},
        },
    )
    value = reply.get("value", reply) if isinstance(reply, Mapping) else reply
    if isinstance(value, Mapping) and set(value) == {"error"}:
        # The fleet has no such server or tool: refuse, never fabricate an answer.
        raise OperationRefused(
            "UNAVAILABLE",
            {
                "reason": f"the fleet does not serve {WORKFLOW_SERVER} {_WORKFLOW_TOOL[op.id]}"
            },
        )
    return {"value": value}


# --- registration -------------------------------------------------------------

#: op id -> (summary, verb, effect, scope, params fields, handler)
_TABLE: dict[str, tuple[str, Verb, Effect, str, dict[str, Any], str]] = {
    "fleet.servers.register": (
        "Register one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name, "config": _Obj},
        "handle_server",
    ),
    "fleet.servers.update": (
        "Update one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name, "config": _Obj},
        "handle_server",
    ),
    "fleet.servers.remove": (
        "Remove one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name},
        "handle_server",
    ),
    "fleet.servers.config.get": (
        "Read one fleet server configuration with secrets as references",
        Verb.ASK,
        Effect.READ,
        "fleet:read",
        {"name": _Name},
        "handle_server",
    ),
    "fleet.skills.toggle": (
        "Enable or disable one skill",
        Verb.WRITE,
        Effect.WRITE,
        "fleet:control",
        {"id": _Name, "enabled": (bool, ...)},
        "handle_engine",
    ),
    "fleet.tools.toggle": (
        "Enable or disable one tool",
        Verb.WRITE,
        Effect.WRITE,
        "fleet:control",
        {"id": _Name, "enabled": (bool, ...)},
        "handle_engine",
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
        "handle_maintenance_status",
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
    if op_id == "workflows.run":
        return "medulla:SubmitWorkItem"
    if op_id in _WORKFLOW_TOOL:
        return "fleet:call"
    if op_id.startswith("fleet.servers."):
        return "medulla:RegisterServer"
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
