"""Skill tier promotion and persona reads served from the engine (ARBORA-OPS-R957).

``skills.tier.promote`` forwards a ``Decide`` kind ``Promotion`` as the verified
caller. The reviewer is the caller, never a field of the request, and the engine
decides which review grant the target tier needs. ``skills.persona.get`` asks a
``Decide`` kind ``LoadSet`` with ``persona``; ``skills.persona.list`` reads the
``Persona`` nodes of the caller's graph. Arbora keeps no tier list and no persona
set. Engine refusals pass through by code (``TIER_REVIEW_MISSING``,
``TIER_SELF_REVIEW``, ``TIER_REVIEW_SCOPE_REQUIRED``,
``ALWAYS_ON_CAPACITY_REACHED``, ``PERSONA_UNKNOWN``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import Field

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.ops.knowledge import EngineReply
from arbora.api.ops.skills_eval import decide_as_caller
from arbora.api.registry import OpSpec, Verb

REVIEW_SCOPE = "skills:review"
READ_SCOPE = "kg:read"
PERSONA_LABEL = "Persona"
_QUESTION_ID = "persona-load-set"


class TierPromoteParams(Params):
    request: dict[str, Any] = Field(
        description="the engine Promotion request, validated by the engine"
    )


class PersonaGetParams(Params):
    persona: str = Field(min_length=1, max_length=256)
    job_text: str = Field(default="persona", min_length=1, max_length=4096)
    max_tools: int | None = Field(default=None, ge=0, le=512)


class PersonaListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


async def handle_tier_promote(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """A tier promotion; the reviewer is the verified caller, decided by the engine."""
    return await decide_as_caller(context, params)


def _feature_schema(context: Any, tenant: str) -> Mapping[str, Any]:
    from arbora.api.task_plan_schema import published_pin

    return published_pin(context.services.get("task_plan_schema_pins"), tenant)


async def handle_persona_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The persona's skill set with the always-on skills first, as the engine ranks it."""
    from medulla.decision_client import load_set_request

    tenant = str(context.caller.tenant)
    question: dict[str, Any] = {
        "job_text": params["job_text"],
        "persona": params["persona"],
        "graph": await caller_dispatch_graph(context),
    }
    if params.get("max_tools") is not None:
        question["max_tools"] = params["max_tools"]
    request = load_set_request(
        tenant, _QUESTION_ID, question, _feature_schema(context, tenant)
    )
    return {"value": await engine_send(context, "Decide", {"request": request})}


async def handle_persona_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The ``Persona`` nodes the caller may see, one page in stable id order."""
    limit = params["limit"]
    rows = await engine_send(
        context,
        "GetNodesByLabel",
        {"label": PERSONA_LABEL, "limit": limit + 1, "after": params.get("cursor")},
    )
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    page = rows[:limit]
    personas = [
        {"id": row[0], "properties": row[1]}
        for row in page
        if isinstance(row, (list, tuple)) and len(row) == 2
    ]
    more = len(rows) > limit and bool(personas)
    return {
        "value": {
            "personas": personas,
            "next_cursor": personas[-1]["id"] if more else None,
        }
    }


def operations() -> tuple[OpSpec, ...]:
    h = "arbora.api.ops.skills_tier.handle_"
    return (
        build_write_op(
            verb=Verb.WRITE,
            op_id="skills.tier.promote",
            summary="Promote a skill to a tier under the caller's review",
            examples=("promote this skill to the domain tier",),
            params=TierPromoteParams,
            result=EngineReply,
            handler=h + "tier_promote",
            scope=REVIEW_SCOPE,
        ),
        build_read_op(
            op_id="skills.persona.get",
            summary="Read the skill set a role persona receives",
            examples=("which skills does the support persona get",),
            params=PersonaGetParams,
            result=EngineReply,
            handler=h + "persona_get",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="skills.persona.list",
            summary="List the role personas",
            examples=("which personas exist",),
            params=PersonaListParams,
            result=EngineReply,
            handler=h + "persona_list",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_persona_get",
    "handle_persona_list",
    "handle_tier_promote",
    "operations",
    "specs",
]
