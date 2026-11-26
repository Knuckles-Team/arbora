"""Trust flags, curation review, catalog usage and the query workspace (ARBORA-OPS-R855.3).

The engine models the glossary, stewardship and catalog as graph facts and serves
them through ``Glossary``, ``Stewardship`` and ``CatalogRead``. Arbora holds no
role table: a review is admitted only when the engine's own ``governance`` answer
for the proposal's target names the verified caller as an owner or steward, and a
caller without that role is refused ``SUBJECT_ACCESS_DENIED`` before any write.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.bootstrap import caller_dispatch_graph
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.registry import OpSpec, Verb
from arbora.pin_lag.medulla import guard, send_main

#: The engine replays each of these writes by its idempotency key.
NATIVE_IDEMPOTENCY: dict[str, str] = {
    "catalog.trust_flag.set": "medulla:Stewardship",
    "curation.proposals.review": "medulla:Glossary",
    "query.workspace.save": "medulla:StoredQueryPut",
    "query.workspace.delete": "medulla:StoredQueryRetire",
    "quality.rule.put": "medulla:QualityRulePut",
    "quality.evaluate": "medulla:AnalyticsJob",
    "profile.run": "medulla:AnalyticsJob",
    "access_request.decide": "medulla:AccessRequestDecide",
}
#: The stored-query methods are read and written under the query scopes.
QUERY_READ_SCOPE = "query:read"
QUERY_WRITE_SCOPE = "query:write"
READ_SCOPE = "kg:read"
WRITE_SCOPE = "kg:write"
#: ``CatalogUsage`` is a ``query:unified`` read; the engine returns principal names
#: only to a caller holding ``catalog:usage:principals`` and counts to the rest.
USAGE_SCOPE = "query:unified"

Flag = Literal["endorsement", "warning", "deprecation"]
ProposalKind = Literal["description", "change_request"]


class TrustFlagParams(Params):
    asset: str = Field(min_length=1, max_length=2048)
    flag: Flag
    reason: str = Field(min_length=1, max_length=500)


class ProposalListParams(Params):
    state: str | None = Field(default=None, max_length=64)
    target: str | None = Field(default=None, max_length=2048)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, max_length=512)


class ProposalReviewParams(Params):
    target: str = Field(min_length=1, max_length=2048)
    proposal_id: str = Field(min_length=1, max_length=2048)
    approve: bool


class UsageParams(Params):
    asset: str = Field(min_length=1, max_length=2048)
    top_n: int | None = Field(default=None, ge=1, le=100)


Language = Literal["uql", "sql", "sparql"]
ParamType = Literal["string", "int", "float", "bool"]
Share = Literal["private", "placement", "tenant"]
RetireReason = Literal["superseded", "deprecated", "incorrect", "unsafe"]


class WorkspaceQueryPutParams(Params):
    """One stored query as the engine's ``StoredQuery`` carries it; the engine sets the author."""

    name: str = Field(min_length=1, max_length=256)
    language: Language
    text: str = Field(min_length=1, max_length=65_536)
    params: dict[str, ParamType] = Field(default_factory=dict, max_length=64)
    materialize: bool = False
    refresh: dict[str, Any] = Field(default_factory=lambda: {"kind": "on_change"})
    share: Share = "private"
    version: int = Field(default=0, ge=0)


class WorkspaceQueryGetParams(Params):
    name: str = Field(min_length=1, max_length=256)
    version: int | None = Field(default=None, ge=0)


class WorkspaceQueryListParams(Params):
    author: str | None = Field(default=None, max_length=256)
    cursor: str | None = Field(default=None, max_length=512)
    include_retired: bool = False
    limit: int | None = Field(default=None, ge=1, le=256)


class WorkspaceQueryDeleteParams(Params):
    name: str = Field(min_length=1, max_length=256)
    reason: RetireReason


class EngineAnswer(Params):
    """An engine answer carried as the engine sent it."""

    model_config = {"extra": "allow"}


def _mapping(answer: Any) -> dict[str, Any]:
    if not isinstance(answer, Mapping):
        raise unavailable("engine result malformed")
    return dict(answer)


async def _proposals(context: Any, request: dict[str, Any]) -> dict[str, Any]:
    answer = _mapping(
        await engine_send(
            context, "CatalogRead", {"op": {"op": "proposals", **request}}
        )
    )
    if not isinstance(answer.get("proposals"), list):
        raise unavailable("engine result malformed")
    return answer


async def handle_trust_flag_set(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One flag per asset with a reason; the engine records and ranks it."""
    action = {
        "op": "set_trust_flag",
        "asset": params["asset"],
        "flag": params["flag"],
        "reason": params["reason"],
    }
    return _mapping(
        await engine_send(context, "Stewardship", {"op": action}, idempotent=True)
    )


async def handle_proposals_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Description proposals and change requests, paged by the engine."""
    request = {k: params[k] for k in ("state", "target", "cursor") if params.get(k)}
    request["limit"] = params["limit"]
    answer = await _proposals(context, request)
    return {"proposals": answer["proposals"], "next_cursor": answer.get("next_cursor")}


async def _is_steward(context: Any, target: str) -> bool:
    """The engine's owners and stewards for ``target`` name the caller."""
    answer = _mapping(
        await engine_send(
            context,
            "CatalogRead",
            {"op": {"op": "governance", "target": {"asset": target}}},
        )
    )
    governance = answer.get("governance")
    if not isinstance(governance, Mapping):
        raise unavailable("engine result malformed")
    roles = {*governance.get("owners", ()), *governance.get("stewards", ())}
    return str(context.caller.principal) in roles


async def handle_proposal_review(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Approve or reject one proposal as a steward the engine names."""
    if not await _is_steward(context, params["target"]):
        raise OperationRefused("SUBJECT_ACCESS_DENIED")
    found = await _proposals(context, {"target": params["target"], "limit": 200})
    proposal = next(
        (
            row
            for row in found["proposals"]
            if isinstance(row, Mapping) and row.get("id") == params["proposal_id"]
        ),
        None,
    )
    if proposal is None:
        raise OperationRefused("NOT_FOUND", {"proposal_id": params["proposal_id"]})
    if proposal.get("kind") == "description":
        method = "Glossary"
        action = {
            "op": "review_description",
            "proposal": params["proposal_id"],
            "approve": params["approve"],
        }
    elif proposal.get("kind") == "change_request":
        method = "Stewardship"
        action = {
            "op": "review",
            "request_id": params["proposal_id"],
            "approve": params["approve"],
        }
    else:
        raise unavailable("engine result malformed")
    receipt = await engine_send(context, method, {"op": action}, idempotent=True)
    return {
        "proposal_id": params["proposal_id"],
        "kind": proposal["kind"],
        "approved": params["approve"],
        "receipt": _mapping(receipt),
    }


async def handle_catalog_usage(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Who queried an asset and how often, as the engine reports it."""
    request: dict[str, Any] = {"asset": params["asset"]}
    if params.get("top_n") is not None:
        request["top_n"] = params["top_n"]
    return _mapping(await engine_send(context, "CatalogUsage", request))


async def handle_workspace_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Publish one immutable version of a stored query under the caller's name."""
    query = {key: params[key] for key in WorkspaceQueryPutParams.model_fields}
    return _mapping(
        await engine_send(context, "StoredQueryPut", {"query": query}, idempotent=True)
    )


async def handle_workspace_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request: dict[str, Any] = {"name": params["name"]}
    if params.get("version") is not None:
        request["version"] = params["version"]
    return _mapping(await engine_send(context, "StoredQueryGet", request))


async def handle_workspace_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = {
        key: params[key]
        for key in ("author", "cursor", "limit")
        if params.get(key) is not None
    }
    request["include_retired"] = bool(params.get("include_retired"))
    return _mapping(await engine_send(context, "StoredQueryList", request))


async def handle_workspace_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request = {"name": params["name"], "reason": params["reason"]}
    return _mapping(
        await engine_send(context, "StoredQueryRetire", request, idempotent=True)
    )


class AccessRequestDecideParams(Params):
    product: str = Field(min_length=1, max_length=2048)
    principal: str = Field(min_length=1, max_length=512)
    approve: bool


async def handle_access_request_decide(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Decide a data-product access request as an owner or steward the engine names.

    Arbora holds no role table: the engine's ``governance`` answer for the product names
    the roles. A caller holding neither refuses ``STEWARD_ROLE_MISSING`` with no effect.
    """
    if not await _is_steward(context, params["product"]):
        raise OperationRefused("STEWARD_ROLE_MISSING")
    return _mapping(
        await engine_send(
            context,
            "AccessRequestDecide",
            {
                "product": params["product"],
                "principal": params["principal"],
                "approve": params["approve"],
                "tenant": str(context.caller.tenant),
            },
            idempotent=True,
        )
    )


QualityDimension = Literal[
    "completeness", "validity", "uniqueness", "consistency", "timeliness", "accuracy"
]
QualitySeverity = Literal["info", "warning", "error", "critical"]


class QualityRulePutParams(Params):
    id: str = Field(min_length=1, max_length=256)
    target: str = Field(min_length=1, max_length=2048)
    dimension: QualityDimension
    expression: str = Field(min_length=1, max_length=4096)
    threshold: float = Field(ge=0.0, le=1.0)
    severity: QualitySeverity


class QualityEvaluateParams(Params):
    target: str = Field(min_length=1, max_length=2048)


class QualityScoresParams(Params):
    target: str = Field(min_length=1, max_length=2048)
    limit: int = Field(default=50, ge=1, le=200)


class ProfileRunParams(Params):
    dataset: str = Field(min_length=1, max_length=2048)
    columns: list[str] = Field(default_factory=list, max_length=256)


class LineageColumnsParams(Params):
    column: str = Field(min_length=1, max_length=2048)
    direction: Literal["upstream", "downstream"] = "downstream"
    max_depth: int | None = Field(default=None, ge=0, le=32)


async def _job(
    context: Any, kind: dict[str, Any], purpose: str, model: str
) -> dict[str, Any]:
    """Submit one analytics job as the verified caller; the engine owns the job and its result."""
    guard("AnalyticsJob", models=(model,))
    graph = await caller_dispatch_graph(context)
    submit = {
        "graph": graph,
        "kind": kind,
        "actor": str(context.caller.principal),
        "purpose": purpose,
    }
    answer = await send_main(
        context,
        "AnalyticsJob",
        {"op": {"Submit": submit}},
        idempotent=True,
        models=(model,),
    )
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_quality_rule_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Put one rule; the engine holds the rule table and the proposed or approved state."""
    rule = {
        key: params[key]
        for key in ("id", "target", "dimension", "expression", "threshold", "severity")
    }
    answer = await send_main(context, "QualityRulePut", {"rule": rule}, idempotent=True)
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


async def handle_quality_evaluate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Submit an evaluation; the engine scores it and opens any incident, never this handler."""
    body = {"target": params["target"], "tenant": str(context.caller.tenant)}
    return await _job(
        context, {"QualityEvaluate": body}, "quality.evaluate", "JobKindQualityEvaluate"
    )


async def handle_profile_run(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    body = {
        "dataset": params["dataset"],
        "columns": list(params.get("columns") or ()),
        "tenant": str(context.caller.tenant),
    }
    return await _job(
        context, {"DatasetProfile": body}, "profile.run", "JobKindDatasetProfile"
    )


_TARGET = re.compile(r"^[A-Za-z0-9_.:/-]{1,256}$")


async def handle_quality_scores(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Read score and incident nodes with ``Uql``: no named quality method exists."""
    target = str(params["target"])
    if _TARGET.fullmatch(target) is None:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "target"})
    limit = int(params.get("limit", 50))
    scores = await engine_send(
        context,
        "Uql",
        {
            "text": f"MATCH (n:QualityScore) |> WHERE target = '{target}' |> LIMIT {limit}"
        },
    )
    incidents = await engine_send(
        context, "Uql", {"text": f"MATCH (n:Incident) |> LIMIT {limit}"}
    )
    return {"value": {"evaluations": scores, "incidents": incidents}}


async def handle_lineage_columns(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "op": "impact",
        "column": params["column"],
        "direction": params.get("direction", "downstream"),
    }
    if params.get("max_depth") is not None:
        operation["max_depth"] = int(params["max_depth"])
    answer = await send_main(context, "ColumnLineage", {"op": operation})
    return {"value": dict(answer) if isinstance(answer, Mapping) else {}}


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.governance"
    return (
        build_write_op(
            op_id="catalog.trust_flag.set",
            summary="Flag an asset as endorsed, warned or deprecated, with a reason",
            examples=("deprecate this table",),
            params=TrustFlagParams,
            result=EngineAnswer,
            handler=f"{module}.handle_trust_flag_set",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="curation.proposals.list",
            summary="List description proposals and change requests awaiting review",
            examples=("list the open curation proposals",),
            params=ProposalListParams,
            result=EngineAnswer,
            handler=f"{module}.handle_proposals_list",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="curation.proposals.review",
            summary="Approve or reject one curation proposal as a steward",
            examples=("approve this glossary description",),
            params=ProposalReviewParams,
            result=EngineAnswer,
            handler=f"{module}.handle_proposal_review",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="catalog.usage",
            verb=Verb.ASK,
            summary="Read which principals query an asset and how often",
            examples=("who uses this table",),
            params=UsageParams,
            result=EngineAnswer,
            handler=f"{module}.handle_catalog_usage",
            scope=USAGE_SCOPE,
        ),
        build_write_op(
            op_id="query.workspace.save",
            summary="Publish a stored query to the shared query workspace",
            examples=("save this query to the workspace",),
            params=WorkspaceQueryPutParams,
            result=EngineAnswer,
            handler=f"{module}.handle_workspace_put",
            scope=QUERY_WRITE_SCOPE,
        ),
        build_read_op(
            op_id="query.workspace.get",
            summary="Read one stored query of the shared query workspace",
            examples=("show this workspace query",),
            params=WorkspaceQueryGetParams,
            result=EngineAnswer,
            handler=f"{module}.handle_workspace_get",
            scope=QUERY_READ_SCOPE,
        ),
        build_read_op(
            op_id="query.workspace.list",
            summary="List the stored queries of the shared query workspace",
            examples=("list the workspace queries",),
            params=WorkspaceQueryListParams,
            result=EngineAnswer,
            handler=f"{module}.handle_workspace_list",
            scope=QUERY_READ_SCOPE,
        ),
        build_write_op(
            op_id="query.workspace.delete",
            summary="Delete (retire) a stored query you authored, with a reason",
            examples=("retire this workspace query",),
            params=WorkspaceQueryDeleteParams,
            result=EngineAnswer,
            handler=f"{module}.handle_workspace_delete",
            scope=QUERY_WRITE_SCOPE,
        ),
        build_write_op(
            op_id="access_request.decide",
            summary="Approve or deny a data-product access request as its steward",
            examples=("approve this access request",),
            params=AccessRequestDecideParams,
            result=EngineAnswer,
            handler=f"{module}.handle_access_request_decide",
            scope="approvals:decide",
        ),
        build_write_op(
            op_id="quality.rule.put",
            summary="Put one data quality rule on a target",
            examples=("require this column to be complete",),
            params=QualityRulePutParams,
            result=EngineAnswer,
            handler=f"{module}.handle_quality_rule_put",
            scope=WRITE_SCOPE,
        ),
        build_write_op(
            op_id="quality.evaluate",
            summary="Evaluate the quality rules of one target",
            examples=("evaluate quality for this table",),
            params=QualityEvaluateParams,
            result=EngineAnswer,
            handler=f"{module}.handle_quality_evaluate",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="quality.scores",
            summary="Read the quality scores and incidents of one target",
            examples=("show the quality scores for this table",),
            params=QualityScoresParams,
            result=EngineAnswer,
            handler=f"{module}.handle_quality_scores",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="profile.run",
            summary="Profile one dataset",
            examples=("profile this dataset",),
            params=ProfileRunParams,
            result=EngineAnswer,
            handler=f"{module}.handle_profile_run",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="lineage.columns",
            summary="Trace the column lineage impact of one column",
            examples=("what depends on this column",),
            params=LineageColumnsParams,
            result=EngineAnswer,
            handler=f"{module}.handle_lineage_columns",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = ["NATIVE_IDEMPOTENCY", "operations", "specs"]
