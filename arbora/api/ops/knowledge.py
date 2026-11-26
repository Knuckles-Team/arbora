"""Knowledge flags and curation served from the engine (ARBORA-OPS-R950, R951).

``knowledge.flag.*`` forwards the engine ``KnowledgeFlag`` method and
``knowledge.curation.*`` forwards ``KnowledgeCuration``. The tenant and the
actor come from the caller's verified graph and claims; a param never names
either. Arbora decides nothing and keeps no flag or curation state: the engine
answer returns unchanged and an engine refusal keeps its own code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_graph_op
from arbora.api.registry import OpSpec, Verb

FLAG_SCOPE = "knowledge:flag"
CURATE_SCOPE = "knowledge:curate"

KnowledgeKind = Literal["skill", "domain", "data_access"]
FlagState = Literal["open", "curated", "dismissed"]
_Text = Field(min_length=1, max_length=4096)
_Id = Field(min_length=1, max_length=512)


class EngineReply(Params):
    """The engine answer, unchanged."""

    value: Any


class FlagRaiseParams(Params):
    knowledge_kind: KnowledgeKind
    flag_kind: Literal["missing", "wrong"]
    target_ref: str = _Id
    what_observed: str = _Text
    run_id: str = _Id
    at_ms: int = Field(ge=0)
    suggested_correction: str | None = Field(default=None, max_length=4096)
    trace_node_id: str | None = Field(default=None, max_length=512)


class FlagResolveParams(Params):
    flag_id: str = _Id
    resolution: Literal["adopt", "dismiss"]
    correction_id: str | None = Field(default=None, max_length=512)
    fixed_by: str | None = Field(default=None, max_length=512)


class FlagGetParams(Params):
    flag_id: str = _Id


class FlagListParams(Params):
    state: FlagState | None = None
    limit: int | None = Field(default=None, ge=1, le=500)


class CurationInputsParams(Params):
    loop_id: str = _Id
    epoch: int = Field(ge=0)
    force: bool = False
    cursor: str | None = Field(default=None, max_length=512)
    limit: int | None = Field(default=None, ge=1, le=500)


class CurationProposeParams(Params):
    loop_id: str = _Id
    epoch: int = Field(ge=0)
    proposal: dict[str, Any]
    body: str = Field(min_length=1, max_length=262144)


class CurationStatusParams(Params):
    proposal_id: str = _Id


class CurationMetricsParams(Params):
    from_ms: int = Field(ge=0)
    to_ms: int = Field(ge=0)


def _present(params: Mapping[str, Any]) -> dict[str, Any]:
    """The params the caller set; an absent optional is not sent."""
    return {key: value for key, value in params.items() if value is not None}


def _forward(
    method: str, name: str, *, write: bool
) -> Callable[[Any, Mapping[str, Any], OpSpec], Awaitable[dict[str, Any]]]:
    async def handle(
        context: Any, params: Mapping[str, Any], op: OpSpec
    ) -> dict[str, Any]:
        answer = await engine_graph_op(
            context, method, {"op": name, **_present(params)}, idempotent=write
        )
        return {"value": answer}

    return handle


handle_flag_raise = _forward("KnowledgeFlag", "raise", write=True)
handle_flag_resolve = _forward("KnowledgeFlag", "resolve", write=True)
handle_flag_get = _forward("KnowledgeFlag", "get", write=False)
handle_flag_list = _forward("KnowledgeFlag", "list", write=False)
handle_curation_inputs = _forward("KnowledgeCuration", "inputs", write=False)
handle_curation_propose = _forward("KnowledgeCuration", "propose", write=True)
handle_curation_status = _forward("KnowledgeCuration", "status", write=False)
handle_curation_metrics = _forward("KnowledgeCuration", "metrics", write=False)

_HANDLERS = "arbora.api.ops.knowledge."


def _read(op_id: str, summary: str, example: str, params: type[Params], scope: str):
    return build_read_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=EngineReply,
        handler=_HANDLERS + _handler_name(op_id),
        scope=scope,
    )


def _write(op_id: str, summary: str, example: str, params: type[Params], scope: str):
    return build_write_op(
        verb=Verb.WRITE,
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=EngineReply,
        handler=_HANDLERS + _handler_name(op_id),
        scope=scope,
    )


def _handler_name(op_id: str) -> str:
    return "handle_" + op_id.removeprefix("knowledge.").replace(".", "_")


def operations() -> tuple[OpSpec, ...]:
    return (
        _write(
            "knowledge.flag.raise",
            "Raise a flag that a skill, term or source is missing or wrong",
            "flag that this skill gave a wrong answer",
            FlagRaiseParams,
            FLAG_SCOPE,
        ),
        _write(
            "knowledge.flag.resolve",
            "Resolve an open knowledge flag by adopting or dismissing it",
            "dismiss this knowledge flag",
            FlagResolveParams,
            CURATE_SCOPE,
        ),
        _read(
            "knowledge.flag.get",
            "Read one knowledge flag with the nodes it links to",
            "show this knowledge flag",
            FlagGetParams,
            FLAG_SCOPE,
        ),
        _read(
            "knowledge.flag.list",
            "List knowledge flags, newest first",
            "list the open knowledge flags",
            FlagListParams,
            FLAG_SCOPE,
        ),
        _read(
            "knowledge.curation.inputs",
            "Read the curation inputs under a live curation loop lease",
            "what is ready for knowledge curation",
            CurationInputsParams,
            CURATE_SCOPE,
        ),
        _write(
            "knowledge.curation.propose",
            "Propose one knowledge edit with the flags it answers",
            "propose this knowledge correction",
            CurationProposeParams,
            CURATE_SCOPE,
        ),
        _read(
            "knowledge.curation.status",
            "Read the status of one curation proposal",
            "what happened to this curation proposal",
            CurationStatusParams,
            CURATE_SCOPE,
        ),
        _read(
            "knowledge.curation.metrics",
            "Read the closed-loop curation measures of a window",
            "how is knowledge curation performing",
            CurationMetricsParams,
            CURATE_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "handle_curation_inputs",
    "handle_curation_metrics",
    "handle_curation_propose",
    "handle_curation_status",
    "handle_flag_get",
    "handle_flag_list",
    "handle_flag_raise",
    "handle_flag_resolve",
    "operations",
    "specs",
]
