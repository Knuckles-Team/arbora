"""Operator, ecosystem and role operations that wait on a served capability.

ARBORA-OPS-R913, R914 and R917 name operations whose engine or connector
surface is not served by the pinned release. Each is registered here with its
exact params and scope, and refuses ``UNAVAILABLE`` with the capability it
waits on; nothing is faked and no local state is kept. A refusal-bound test
walks the table. An operation leaves the table when its surface is pinned and
its handler is written in the module that owns it.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from typing import Any, Literal

from pydantic import Field, create_model

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.knowledge import EngineReply
from arbora.api.registry import Effect, OpSpec, Verb

_Name = (str, Field(min_length=1, max_length=256))
_OptName = (str | None, Field(default=None, min_length=1, max_length=256))
_Obj: tuple[Any, Any] = (dict[str, Any], Field(default_factory=dict))
_Limit = (int, Field(default=50, ge=1, le=200))
_Cursor = (str | None, Field(default=None, max_length=1024))

#: op id -> (summary, verb, effect, scope, params fields, what it waits on)
_PENDING: dict[str, tuple[str, Verb, Effect, str, dict[str, Any], str]] = {
    "fleet.servers.register": (
        "Register one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name, "config": _Obj},
        "the fleet server registry write behind plan confirmation",
    ),
    "fleet.servers.update": (
        "Update one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name, "config": _Obj},
        "the fleet server registry write behind plan confirmation",
    ),
    "fleet.servers.remove": (
        "Remove one fleet server",
        Verb.WRITE,
        Effect.ADMIN,
        "fleet:control",
        {"name": _Name},
        "the fleet server registry write behind plan confirmation",
    ),
    "fleet.servers.config.get": (
        "Read one fleet server configuration with secrets as references",
        Verb.ASK,
        Effect.READ,
        "fleet:read",
        {"name": _Name},
        "the fleet server registry read that returns secret references",
    ),
    "fleet.skills.toggle": (
        "Enable or disable one skill",
        Verb.WRITE,
        Effect.WRITE,
        "fleet:control",
        {"id": _Name, "enabled": (bool, ...)},
        "the skill pin operations as one toggle",
    ),
    "fleet.tools.toggle": (
        "Enable or disable one tool",
        Verb.WRITE,
        Effect.WRITE,
        "fleet:control",
        {"id": _Name, "enabled": (bool, ...)},
        "the tool pin operations as one toggle",
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
        "the engine schedule calendar read",
    ),
    "ops.maintenance.status": (
        "Read the maintenance status",
        Verb.ASK,
        Effect.READ,
        "ops:read",
        {},
        "the engine Maintain status read",
    ),
    "ops.maintenance.run": (
        "Run one maintenance cycle",
        Verb.WRITE,
        Effect.ADMIN,
        "ops:admin",
        {},
        "the engine Maintain run",
    ),
    "ingest.pipeline.status": (
        "Read the ingest pipeline status",
        Verb.ASK,
        Effect.READ,
        "ingest:read",
        {},
        "the ingest job status read under one name",
    ),
    "ingest.pipeline.run": (
        "Run the ingest pipeline",
        Verb.WRITE,
        Effect.WRITE,
        "ingest:write",
        {"source": _OptName},
        "the ingest submission create under one name",
    ),
    "workflows.list": (
        "List workflows",
        Verb.FIND,
        Effect.READ,
        "work:read",
        {"limit": _Limit, "cursor": _Cursor},
        "the business-process connector workflow list",
    ),
    "workflows.create": (
        "Create a workflow",
        Verb.WRITE,
        Effect.WRITE,
        "work:write",
        {"definition": _Obj},
        "the business-process connector workflow write",
    ),
    "workflows.run": (
        "Run a workflow",
        Verb.WRITE,
        Effect.WRITE,
        "work:write",
        {"workflow_id": _Name, "inputs": _Obj},
        "the business-process connector run and its run machine",
    ),
    "code.context.read": (
        "Read code navigation context",
        Verb.ASK,
        Effect.READ,
        "node:read",
        {"action": (Literal["nav", "instances"], ...), "symbol": _OptName},
        "the code index read",
    ),
    "commands.execute": (
        "Execute a command that is a served operation",
        Verb.WRITE,
        Effect.WRITE,
        "registry:read",
        {"command": _Name, "args": _Obj},
        "the registry-only command dispatcher",
    ),
    "commands.autocomplete": (
        "Complete a command from the served operations",
        Verb.FIND,
        Effect.READ,
        "registry:read",
        {"prefix": (str, Field(default="", max_length=256))},
        "the registry-only command list",
    ),
    "resources.list": (
        "List resources",
        Verb.FIND,
        Effect.READ,
        "capacity:read",
        {},
        "the capacity status read as a resource list",
    ),
    "resources.spawn": (
        "Spawn a resource",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"kind": _Name, "config": _Obj},
        "the agent run start as a resource spawn",
    ),
    "ecosystem.widgets.read": (
        "Read one ecosystem widget",
        Verb.ASK,
        Effect.READ,
        "fleet:read",
        {"widget": _Name, "params": _Obj},
        "the ecosystem widget registry and its connector calls",
    ),
    "roles.templates.list": (
        "List role templates",
        Verb.FIND,
        Effect.READ,
        "registry:read",
        {"limit": _Limit, "cursor": _Cursor},
        "the engine agent-template list op",
    ),
    "roles.templates.get": (
        "Read one role template",
        Verb.ASK,
        Effect.READ,
        "registry:read",
        {"template_id": _Name},
        "the engine agent-template current read in a pinned release",
    ),
    "roles.instances.create": (
        "Bind a role template to the tenant profile",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"template_id": _Name, "binding": _Obj},
        "the engine tenant business profile and the template instantiate op",
    ),
    "roles.instances.update": (
        "Narrow a role instance binding",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"instance_id": _Name, "binding": _Obj},
        "the engine tenant business profile and the library write",
    ),
    "roles.instances.retire": (
        "Retire a role instance",
        Verb.WRITE,
        Effect.WRITE,
        "agent:component-write",
        {"instance_id": _Name},
        "the engine agent library retire for role instances",
    ),
}


async def handle_pending(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Refuse ``UNAVAILABLE`` naming the capability the operation waits on."""
    waits = _PENDING[op.id][5]
    raise OperationRefused("UNAVAILABLE", {"reason": f"waits on {waits}"})


def pending_ids() -> tuple[str, ...]:
    return tuple(_PENDING)


@cache
def operations() -> tuple[OpSpec, ...]:
    specs: list[OpSpec] = []
    for op_id, (summary, verb, effect, scope, fields, _waits) in _PENDING.items():
        model = create_model(
            "Params_" + op_id.replace(".", "_"),
            __base__=Params,
            **fields,
        )
        common: dict[str, Any] = {
            "op_id": op_id,
            "summary": summary,
            "examples": (summary.lower(),),
            "params": model,
            "result": EngineReply,
            "handler": "arbora.api.ops.operator_surface.handle_pending",
            "scope": scope,
        }
        if effect is Effect.READ:
            specs.append(build_read_op(verb=verb, **common))
        else:
            specs.append(build_write_op(verb=verb, effect=effect, **common))
    return tuple(specs)


specs = operations
