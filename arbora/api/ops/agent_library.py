"""Agent Library operations over the engine ``AgentLibrary`` method (ARBORA-OPS-R912).

The tenant Agent Library is the engine's own record: arbora forwards each call
as the verified caller and keeps no item. The engine library holds agents only,
so a ``kind`` of ``tool`` refuses ``UNAVAILABLE`` until the
engine serves a kind read. ``list`` and ``summary`` need the engine list op;
until the pinned client carries it they refuse ``UNAVAILABLE`` as well, and a
pinned release that serves it needs no arbora change.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field, ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import OpSpec

READ_SCOPE = "agent:library-read"
WRITE_SCOPE = "agent:library-write"
_PAGE = 200
_SUMMARY_PAGES = 50
_ID = Field(min_length=1, max_length=256)
Kind = Literal["agent", "tool"]
Lifecycle = Literal["published", "retired", "withdrawn"]


class LibraryResult(Params):
    value: dict[str, Any]


class ListParams(Params):
    kind: Kind = "agent"
    lifecycle: Lifecycle | None = None
    limit: int = Field(default=50, ge=1, le=_PAGE)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class GetParams(Params):
    id: str = _ID
    kind: Kind = "agent"


class WriteParams(Params):
    item: dict[str, Any]
    kind: Kind = "agent"
    expected_revision: int | None = Field(default=None, ge=0)


class DeleteParams(Params):
    id: str = _ID
    kind: Kind = "agent"
    expected_revision: int | None = Field(default=None, ge=0)


class SummaryParams(Params):
    kind: Kind = "agent"


class A2aParams(Params):
    id: str = _ID


def _only_agents(kind: str) -> None:
    if kind != "agent":
        raise unavailable("the engine library serves agents only; no kind read yet")


def _list_op_served() -> bool:
    """Whether the pinned engine client declares the library list op."""
    from medulla.generated import models

    return getattr(models, "AgentLibraryOpList", None) is not None


def _tenant(context: Any) -> str:
    return str(context.caller.tenant)


async def _library(
    context: Any, op: dict[str, Any], *, write: bool = False
) -> dict[str, Any]:
    answer = await engine_send(context, "AgentLibrary", {"op": op}, idempotent=write)
    if not isinstance(answer, Mapping):
        raise unavailable("the engine answered the library with an unknown shape")
    return dict(answer)


async def _page(context: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    if not _list_op_served():
        raise unavailable("the pinned engine does not serve the library list op")
    op: dict[str, Any] = {
        "operation": "list",
        "tenant_id": _tenant(context),
        "limit": int(params.get("limit") or _PAGE),
    }
    for field in ("lifecycle", "cursor"):
        if params.get(field):
            op[field] = params[field]
    return await _library(context, op)


async def handle_agents_library_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _only_agents(params.get("kind", "agent"))
    page = await _page(context, params)
    return {
        "value": {
            "items": page.get("entries", []),
            "next_cursor": page.get("next_cursor"),
        }
    }


async def handle_agents_library_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _only_agents(params.get("kind", "agent"))
    entry = await _current(context, params["id"])
    return {"value": {"items": [entry], "revision": entry.get("entry_revision")}}


async def _current(
    context: Any, agent_id: str, *, missing: str = "NOT_FOUND"
) -> dict[str, Any]:
    """One agent's head entry; an absent or foreign agent is one ``NOT_FOUND``."""
    answer = await _library(
        context,
        {"operation": "current", "agent_id": agent_id, "tenant_id": _tenant(context)},
    )
    entry = answer.get("entry", answer)
    if not isinstance(entry, Mapping) or not entry.get("agent_id"):
        raise OperationRefused(missing)
    return dict(entry)


async def current_entry(context: Any, agent_id: str) -> dict[str, Any]:
    """One agent's head library entry as the verified caller (a public read for gates)."""
    return await _current(context, agent_id)


def _draft(item: Mapping[str, Any], context: Any) -> dict[str, Any]:
    """The item as a validated engine draft in the caller's tenant."""
    from medulla.generated.decision import AgentLibraryEntryDraft

    try:
        draft = AgentLibraryEntryDraft.model_validate(
            {**item, "tenant_id": _tenant(context)}
        )
    except ValidationError as exc:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"errors": exc.error_count()}
        ) from exc
    return draft.model_dump(mode="json")


def _mutation_context(
    context: Any, purpose: str, expected: int | None
) -> dict[str, Any]:
    from arbora.mcp_server.decide_wiring import _mutation_context as build

    return {**build(context.caller.session, purpose), "expected_revision": expected}


async def _write(
    context: Any, params: Mapping[str, Any], *, expect_existing: bool
) -> dict[str, Any]:
    _only_agents(params.get("kind", "agent"))
    draft = _draft(params["item"], context)
    expected = params.get("expected_revision")
    if expect_existing and expected is None:
        raise OperationRefused("REVISION_CONFLICT")
    request = {
        "context": _mutation_context(context, "agent_library.publish", expected),
        "entry": draft,
    }
    answer = await _library(
        context, {"operation": "publish", "request": request}, write=True
    )
    entry = answer.get("entry", {})
    return {"value": {"item": entry, "revision": entry.get("entry_revision")}}


async def publish_entry(
    context: Any, item: Mapping[str, Any], *, expected_revision: int | None = None
) -> dict[str, Any]:
    """Publish one agent entry as the verified caller (a public write for the host)."""
    answer = await _write(
        context,
        {"item": item, "expected_revision": expected_revision},
        expect_existing=expected_revision is not None,
    )
    return dict(answer["value"]["item"])


async def handle_agents_library_create(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _write(context, params, expect_existing=False)


async def handle_agents_library_update(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    return await _write(context, params, expect_existing=True)


async def handle_agents_library_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    _only_agents(params.get("kind", "agent"))
    request = {
        "agent_id": params["id"],
        "context": _mutation_context(
            context, "agent_library.retire", params.get("expected_revision")
        ),
    }
    await _library(context, {"operation": "retire", "request": request}, write=True)
    return {"value": {"id": params["id"], "retired": True}}


async def handle_agents_library_summary(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Counts by lifecycle, paged through the engine list until it is exhausted."""
    _only_agents(params.get("kind", "agent"))
    counts: dict[str, int] = {}
    cursor: str | None = None
    for _ in range(_SUMMARY_PAGES):
        page = await _page(context, {"limit": _PAGE, "cursor": cursor})
        for entry in page.get("entries", []):
            lifecycle = str(entry.get("lifecycle", "unknown"))
            counts[lifecycle] = counts.get(lifecycle, 0) + 1
        cursor = page.get("next_cursor")
        if not cursor:
            return {"value": {"counts": counts, "total": sum(counts.values())}}
    raise unavailable("the library is larger than one summary can count")


async def handle_agents_library_a2a(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The A2A card of one published agent, built from its library entry."""
    entry = await _current(context, params["id"])
    if entry.get("lifecycle") != "published":
        raise OperationRefused("NOT_FOUND")
    skills = [
        {"id": str(s["component_id"]), "name": str(s["component_id"])}
        for s in entry.get("skills", [])
        if isinstance(s, Mapping) and "component_id" in s
    ]
    card = {
        "name": entry["agent_id"],
        "description": entry.get("role", ""),
        "version": entry.get("version", ""),
        "skills": skills,
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["text/plain"],
    }
    return {"value": {"agent_card": card}}


def _read(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
    return build_read_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=LibraryResult,
        handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
        scope=READ_SCOPE,
    )


def _mutate(op_id: str, summary: str, example: str, params: type[Params]) -> OpSpec:
    return build_write_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=LibraryResult,
        handler=f"{__name__}.handle_{op_id.replace('.', '_')}",
        scope=WRITE_SCOPE,
    )


#: Writes the engine replays by the idempotency key the pipeline forwards.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "agents.library.create": "medulla:AgentLibrary",
    "agents.library.update": "medulla:AgentLibrary",
    "agents.library.delete": "medulla:AgentLibrary",
}


def operations() -> tuple[OpSpec, ...]:
    return (
        _read(
            "agents.library.list",
            "List the tenant Agent Library",
            "list the agent library",
            ListParams,
        ),
        _read(
            "agents.library.get",
            "Read one library agent",
            "show this library agent",
            GetParams,
        ),
        _read(
            "agents.library.summary",
            "Count the library by lifecycle",
            "how many agents are in the library",
            SummaryParams,
        ),
        _read(
            "agents.library.a2a",
            "Read the A2A card of one library agent",
            "agent card for this library agent",
            A2aParams,
        ),
        _mutate(
            "agents.library.create",
            "Publish a new library agent",
            "add an agent to the library",
            WriteParams,
        ),
        _mutate(
            "agents.library.update",
            "Publish a new revision of a library agent",
            "update this library agent",
            WriteParams,
        ),
        _mutate(
            "agents.library.delete",
            "Retire a library agent",
            "retire this library agent",
            DeleteParams,
        ),
    )


specs = operations
