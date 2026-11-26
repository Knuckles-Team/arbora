"""Ontology object sets, actions and edits over the engine (ARBORA-OPS-R908, R922.1).

Handlers group by the engine surface they ride on and hold no state:

- UQL reads (``search``, ``search_around``, ``pivot``, ``aggregate``, ``get``)
  compile a statement whose every value travels as a bound parameter; a type,
  link or field name is checked against one identifier pattern first.
- Object sets and object views are records in the engine KV namespaces
  ``object_sets`` and ``object_views`` under the caller's own session.
- ``edit`` and ``revert`` go through the engine ``GovernedChange`` method; the
  proposed changes are staged in the ``object_edits`` namespace by change id.
- ``actions.apply`` submits each object through the engine ``ActionSubmit``.
- ``document.process`` calls the engine ``EnrichDocument``.
- ``interfaces.*`` read the marker on the engine schema class rows.

``actions.list``, ``functions.invoke`` and ``derive`` have no engine surface on
the engine main branch yet and refuse ``UNAVAILABLE``; the shapes wanted are in
the gaps list.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops._engine import engine_send, unavailable
from arbora.api.ops._pending import refuse_unavailable, unserved_write
from arbora.api.ops.knowledge import EngineReply
from arbora.api.ops.memory import _decode, _encode, _payload, _send
from arbora.api.registry import Effect, OpSpec, Verb

handle_unserved = refuse_unavailable
READ_SCOPE = "kg:read"
WRITE_SCOPE = "kg:write"
KV_READ = "kv:read"
KV_WRITE = "kv:write"
SETS_NAMESPACE = "object_sets"
VIEWS_NAMESPACE = "object_views"
EDITS_NAMESPACE = "object_edits"
_NAME = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"
_SET_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:@-]{0,127}$"
_ENGINE_SCHEMA_PAGE = 200
_SCHEMA_PAGES = 20
_EDIT_TTL_MS = 7 * 24 * 3_600_000
_EDIT_WINDOW_MS = 3_600_000

Scalar = str | int | float | bool
Aggregate = Literal["count", "sum", "min", "max", "avg"]


class Metric(Params):
    op: Aggregate
    field: str | None = Field(default=None, pattern=_NAME)


class SearchParams(Params):
    object_type: str = Field(pattern=_NAME)
    filter: dict[str, Scalar] | None = Field(default=None, max_length=16)
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, pattern=r"^[0-9]{1,6}$")


class SearchAroundParams(Params):
    object_ids: list[str] = Field(min_length=1, max_length=100)
    link_type: str = Field(pattern=_NAME)
    hops: int = Field(default=1, ge=1, le=3)


class PivotParams(Params):
    object_set_id: str | None = Field(default=None, pattern=_SET_ID)
    object_ids: list[str] | None = Field(default=None, min_length=1, max_length=100)
    link_type: str = Field(pattern=_NAME)


class AggregateParams(Params):
    object_type: str = Field(pattern=_NAME)
    group_by: list[str] = Field(default_factory=list, max_length=4)
    metrics: list[Metric] = Field(min_length=1, max_length=8)


class ObjectIdParams(Params):
    object_id: str = Field(min_length=1, max_length=512)


class SetSaveParams(Params):
    name: str = Field(pattern=_SET_ID)
    definition: dict[str, Any]


class SetNameParams(Params):
    name: str = Field(pattern=_SET_ID)


class SetListParams(Params):
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, max_length=1_024)


class EditParams(ObjectIdParams):
    changes: dict[str, Any] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=2_000)


class RevertParams(Params):
    change_id: str = Field(min_length=1, max_length=256)


class ActionApplyParams(Params):
    action: str = Field(pattern=_NAME)
    object_ids: list[str] = Field(min_length=1, max_length=100)
    params: dict[str, Scalar] = Field(default_factory=dict, max_length=32)


class DocumentParams(Params):
    document_id: str = Field(min_length=1, max_length=512)


class ViewGetParams(Params):
    object_type: str = Field(pattern=_NAME)


class ViewPutParams(ViewGetParams):
    layout: dict[str, Any]


class FunctionParams(Params):
    function: str = Field(pattern=_NAME)
    args: dict[str, Any] = Field(default_factory=dict)


class DeriveParams(Params):
    object_type: str = Field(pattern=_NAME)
    expression: str = Field(min_length=1, max_length=2_000)
    limit: int = Field(default=50, ge=1, le=200)


class InterfacesListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class ImplementersParams(InterfacesListParams):
    name: str = Field(min_length=1, max_length=256)


class Reply(Params):
    value: dict[str, Any]


# --- UQL reads --------------------------------------------------------------


def _wire(value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"Bool": value}
    if isinstance(value, str):
        return {"Str": value}
    return {"Num": float(value)}


class _Statement:
    """One UQL text with the values it binds; a value never enters the text."""

    def __init__(self) -> None:
        self.params: dict[str, dict[str, Any]] = {}

    def bind(self, value: Any) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = _wire(value)
        return f"${name}"

    def ids(self, object_ids: Sequence[str]) -> str:
        name = f"p{len(self.params)}"
        self.params[name] = {"List": [{"Str": item} for item in object_ids]}
        return f"id IN ${name}"

    def where(self, filters: Mapping[str, Scalar] | None) -> str:
        if not filters:
            return ""
        terms = " AND ".join(
            f"{k} = {self.bind(v)}" for k, v in sorted(filters.items())
        )
        return f" |> WHERE {terms}"

    def request(self, text: str) -> dict[str, Any]:
        return {"text": text, "params": self.params}


async def _rows(context: Any, statement: _Statement, text: str) -> list[Any]:
    answer = await engine_send(context, "Uql", statement.request(text))
    body = answer.get("Rows") if isinstance(answer, Mapping) else None
    rows = body.get("rows") if isinstance(body, Mapping) else None
    if not isinstance(rows, list):
        raise unavailable("the engine answered a query with an unknown shape")
    return rows


async def handle_derive(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Preview a derived property: the expression runs as UQL through ``Uql``.

    Read-only and bounded by a row limit; the engine's UQL budget applies. An
    expression that does not start a query continues the object type's match.
    """
    expression = str(params["expression"]).strip()
    text = (
        expression
        if expression.upper().startswith("MATCH")
        else f"MATCH (:{params['object_type']}) |> {expression}"
    )
    limit = int(params.get("limit", 50))
    rows = await _rows(context, _Statement(), f"{text} |> LIMIT {limit}")
    return {"value": {"preview": rows, "limit": limit}}


async def handle_actions_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The registered actions; ``UNAVAILABLE`` until the pinned engine has ``ActionList``."""
    answer = await engine_send(context, "ActionList", {})
    return {"value": answer if isinstance(answer, Mapping) else {"actions": []}}


async def handle_objects_search(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Objects of one type matching equality filters; keyset-free skip paging."""
    statement = _Statement()
    skip = int(params.get("cursor") or 0)
    limit = int(params["limit"])
    text = f"MATCH (:{params['object_type']}){statement.where(params.get('filter'))}"
    text += f" |> ORDER BY id |> SKIP {skip} |> LIMIT {limit + 1}"
    rows = await _rows(context, statement, text)
    more = len(rows) > limit
    return {
        "value": {
            "objects": rows[:limit],
            "truncated": more,
            "next_cursor": str(skip + limit) if more else None,
        }
    }


async def handle_objects_search_around(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    statement = _Statement()
    seed = statement.ids(params["object_ids"])
    hops = int(params["hops"])
    text = (
        f"MATCH () |> WHERE {seed} |> TRAVERSE -[:{params['link_type']}]-{{1,{hops}}}"
    )
    return {"value": {"objects": await _rows(context, statement, text)}}


async def _set_definition(context: Any, set_id: str) -> dict[str, Any]:
    found = await _kv_get(context, SETS_NAMESPACE, set_id)
    if found is None:
        raise OperationRefused("NOT_FOUND")
    return found[0].get("definition", {})


async def handle_objects_pivot(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Move from a set of objects (saved or listed) across one link type."""
    ids, set_id = params.get("object_ids"), params.get("object_set_id")
    if bool(ids) == bool(set_id):
        raise OperationRefused("INVALID_ARGUMENT", {"field": "object_set_id"})
    statement = _Statement()
    if ids:
        source = f"MATCH () |> WHERE {statement.ids(ids)}"
    else:
        definition = await _set_definition(context, str(set_id))
        kind = definition.get("object_type")
        if not isinstance(kind, str) or not _matches(_NAME, kind):
            raise OperationRefused("INVALID_ARGUMENT", {"field": "definition"})
        source = f"MATCH (:{kind}){statement.where(_scalars(definition.get('filter')))}"
    text = f"{source} |> TRAVERSE -[:{params['link_type']}]-{{1,1}}"
    return {"value": {"objects": await _rows(context, statement, text)}}


def _matches(pattern: str, value: str) -> bool:
    import re

    return re.fullmatch(pattern, value) is not None


def _scalars(value: Any) -> dict[str, Scalar]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(k): v
        for k, v in value.items()
        if _matches(_NAME, str(k)) and isinstance(v, (str, int, float, bool))
    }


def _aggregate(metric: Mapping[str, Any]) -> str:
    op, field = str(metric["op"]).upper(), metric.get("field")
    if op == "COUNT":
        return "COUNT AS count"
    if not field:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "metrics"})
    return f"{op}({field}) AS {metric['op']}_{field}"


async def handle_objects_aggregate(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    statement = _Statement()
    keys = ", ".join(params.get("group_by") or ()) or "label"
    aggs = ", ".join(_aggregate(dict(m)) for m in params["metrics"])
    text = f"MATCH (:{params['object_type']}) |> GROUP BY {keys} AGGREGATE {aggs} |> LIMIT 1000"
    return {"value": {"groups": await _rows(context, statement, text)}}


async def handle_objects_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One object by id and the nodes one hop from it; absent and foreign agree."""
    node = _Statement()
    found = await _rows(
        context,
        node,
        f"MATCH () |> WHERE id = {node.bind(params['object_id'])} |> LIMIT 1",
    )
    if not found:
        raise OperationRefused("NOT_FOUND")
    links = _Statement()
    seed = links.ids([params["object_id"]])
    around = await _rows(
        context, links, f"MATCH () |> WHERE {seed} |> TRAVERSE -[]-{{1,1}} |> LIMIT 200"
    )
    return {"value": {"object": found[0], "links": around}}


# --- KV records: object sets and views --------------------------------------


async def _kv_get(
    context: Any, namespace: str, key: str
) -> tuple[dict[str, Any], int] | None:
    entry = _payload(
        await _send(
            context,
            "KvGet",
            {"namespace": namespace, "key": key, "shared": False},
            None,
        )
    )
    if not isinstance(entry, Mapping) or "value" not in entry:
        return None
    return _decode(entry["value"]), int(entry.get("version", 0))


async def _kv_cas(
    context: Any, namespace: str, key: str, expected: int, value: Mapping[str, Any]
) -> int:
    base = getattr(context, "idempotency_key", None)
    receipt = _payload(
        await _send(
            context,
            "KvCas",
            {
                "namespace": namespace,
                "key": key,
                "shared": False,
                "expected_version": expected,
                "new": _encode(value),
            },
            f"{base}:{namespace}" if base else None,
        )
    )
    if not (isinstance(receipt, Mapping) and receipt.get("swapped")):
        raise OperationRefused("REVISION_CONFLICT", {"expected_revision": expected})
    return int(receipt.get("version", 0))


async def handle_object_sets_save(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    name = params["name"]
    current = await _kv_get(context, SETS_NAMESPACE, name)
    revision = await _kv_cas(
        context,
        SETS_NAMESPACE,
        name,
        current[1] if current else 0,
        {"definition": params["definition"]},
    )
    return {"value": {"id": name, "revision": revision}}


async def handle_object_sets_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "namespace": SETS_NAMESPACE,
        "prefix": "",
        "limit": int(params["limit"]),
        "shared": False,
    }
    if params.get("cursor"):
        request["cursor"] = params["cursor"]
    page = _payload(await _send(context, "KvScan", request, None))
    rows = page.get("entries", []) if isinstance(page, Mapping) else []
    sets = [
        {
            "id": str(row["key"]),
            "definition": _decode(row.get("value")).get("definition", {}),
            "revision": int(row.get("version", 0)),
        }
        for row in rows
        if isinstance(row, Mapping) and "key" in row
    ]
    cursor = page.get("next_cursor") if isinstance(page, Mapping) else None
    return {"value": {"object_sets": sets, "next_cursor": cursor}}


async def handle_object_sets_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    if await _kv_get(context, SETS_NAMESPACE, params["name"]) is None:
        raise OperationRefused("NOT_FOUND")
    await _send(
        context,
        "KvDelete",
        {"namespace": SETS_NAMESPACE, "key": params["name"], "shared": False},
        getattr(context, "idempotency_key", None),
    )
    return {"value": {"id": params["name"], "deleted": True}}


async def handle_object_views_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    found = await _kv_get(context, VIEWS_NAMESPACE, params["object_type"])
    if found is None:
        raise OperationRefused("NOT_FOUND")
    return {"value": {"object_type": params["object_type"], "layout": found[0]}}


async def handle_object_views_put(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    key = params["object_type"]
    current = await _kv_get(context, VIEWS_NAMESPACE, key)
    revision = await _kv_cas(
        context, VIEWS_NAMESPACE, key, current[1] if current else 0, params["layout"]
    )
    return {
        "value": {"object_type": key, "layout": params["layout"], "revision": revision}
    }


# --- governed edits ---------------------------------------------------------


def _digest(object_id: str, changes: Mapping[str, Any]) -> str:
    body = json.dumps([object_id, changes], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


async def handle_objects_edit(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Propose the change through the engine; the object is never written here."""
    digest = _digest(params["object_id"], params["changes"])
    change_id = f"object-edit-{digest[:32]}"
    await _staged(context, change_id, params)
    answer = await engine_send(
        context,
        "GovernedChange",
        {
            "op": {
                "op": "propose",
                "request": {
                    "change_id": change_id,
                    "digest": f"sha256:{digest}",
                    "justification": params["reason"],
                    "kind": "object_edit",
                    "proposal_ttl_ms": _EDIT_TTL_MS,
                    "target": params["object_id"],
                    "window_ms": _EDIT_WINDOW_MS,
                },
            }
        },
        idempotent=True,
    )
    return {"value": {"change_id": change_id, "state": _state(answer, "proposed")}}


async def _staged(context: Any, change_id: str, params: Mapping[str, Any]) -> None:
    """Stage the change body once; a replay of the same edit finds it present."""
    if await _kv_get(context, EDITS_NAMESPACE, change_id) is not None:
        return
    await _kv_cas(
        context,
        EDITS_NAMESPACE,
        change_id,
        0,
        {"object_id": params["object_id"], "changes": params["changes"]},
    )


def _state(answer: Any, default: str) -> str:
    if isinstance(answer, Mapping):
        value = answer.get("state") or answer.get("status")
        if isinstance(value, str):
            return value
    return default


async def handle_objects_revert(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    answer = await engine_send(
        context,
        "GovernedChange",
        {"op": {"op": "revoke", "change_id": params["change_id"]}},
        idempotent=True,
    )
    return {
        "value": {"change_id": params["change_id"], "state": _state(answer, "revoked")}
    }


# --- actions and documents --------------------------------------------------


async def handle_actions_apply(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Submit the named action once per object; each result keeps its own refusal."""
    results = []
    for object_id in params["object_ids"]:
        body = {
            "action": params["action"],
            "params": {**params["params"], "object_id": object_id},
        }
        results.append(
            {
                "object_id": object_id,
                "result": await engine_send(
                    context, "ActionSubmit", body, idempotent=True
                ),
            }
        )
    return {"value": {"results": results}}


async def handle_document_process(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    answer = await engine_send(
        context,
        "EnrichDocument",
        {"request": {"document_id": params["document_id"]}},
        idempotent=True,
    )
    job = answer.get("enrichment_id") if isinstance(answer, Mapping) else None
    if not isinstance(job, str):
        raise unavailable("the engine answered enrichment with an unknown shape")
    return {"value": {"job_id": job}}


# --- interfaces (R922.1) ----------------------------------------------------


def _local(term: Mapping[str, Any]) -> str:
    return str(term.get("local_name") or term.get("iri", ""))


def _check_marked(terms: Sequence[Mapping[str, Any]]) -> None:
    if terms and not any("interface" in term for term in terms):
        raise unavailable("the engine class rows carry no interface marker yet")


async def _schema_page(
    context: Any, cursor: str | None, limit: int
) -> tuple[list[dict[str, Any]], str | None]:
    request: dict[str, Any] = {"limit": limit, "kind": "class"}
    if cursor:
        request["cursor"] = cursor
    answer = await engine_send(context, "GraphSchemaClasses", request)
    terms = answer.get("terms") if isinstance(answer, Mapping) else None
    if not isinstance(terms, list):
        raise unavailable("the engine answered the schema with an unknown shape")
    _check_marked(terms)
    return terms, answer.get("next_cursor")


async def handle_interfaces_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One engine page of class rows filtered to interfaces; a page may be empty."""
    terms, cursor = await _schema_page(
        context, params.get("cursor"), int(params["limit"])
    )
    found = [
        {"name": _local(t), "iri": t.get("iri"), "source_id": t.get("source_id")}
        for t in terms
        if t.get("interface")
    ]
    return {
        "value": {
            "interfaces": found,
            "truncated": cursor is not None,
            "next_cursor": cursor,
        }
    }


async def _all_terms(context: Any) -> list[dict[str, Any]]:
    terms: list[dict[str, Any]] = []
    cursor: str | None = None
    for _ in range(_SCHEMA_PAGES):
        page, cursor = await _schema_page(context, cursor, _ENGINE_SCHEMA_PAGE)
        terms.extend(page)
        if cursor is None:
            return terms
    raise unavailable("the schema is larger than one interface read can scan")


async def handle_interfaces_implementers(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    terms = await _all_terms(context)
    wanted = params["name"]
    target = next(
        (
            t
            for t in terms
            if t.get("interface") and wanted in (_local(t), t.get("iri"))
        ),
        None,
    )
    if target is None:
        raise OperationRefused("INTERFACE_NOT_FOUND")
    names = {_local(target), target.get("iri")}
    implementers = [
        {"class_name": _local(t), "iri": t.get("iri")}
        for t in terms
        if names & set(t.get("implements") or ())
    ]
    start = (
        int(params.get("cursor") or 0)
        if str(params.get("cursor") or "0").isdigit()
        else 0
    )
    limit = int(params["limit"])
    page = implementers[start : start + limit]
    more = start + limit < len(implementers)
    return {
        "value": {
            "implementers": page,
            "truncated": more,
            "next_cursor": str(start + limit) if more else None,
        }
    }


# --- registration -----------------------------------------------------------


def _read(
    op_id: str,
    summary: str,
    example: str,
    params: type[Params],
    scope: str = READ_SCOPE,
) -> OpSpec:
    return build_read_op(
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=Reply,
        handler=f"{__name__}.handle_{_suffix(op_id)}",
        scope=scope,
    )


def _write(
    op_id: str,
    summary: str,
    example: str,
    params: type[Params],
    scope: str = WRITE_SCOPE,
    verb: Verb = Verb.WRITE,
) -> OpSpec:
    return build_write_op(
        verb=verb,
        effect=Effect.WRITE,
        op_id=op_id,
        summary=summary,
        examples=(example,),
        params=params,
        result=Reply,
        handler=f"{__name__}.handle_{_suffix(op_id)}",
        scope=scope,
    )


def _suffix(op_id: str) -> str:
    return op_id.removeprefix("ontology.").replace(".", "_")


def operations() -> tuple[OpSpec, ...]:
    m = __name__
    return (
        _read(
            "ontology.interfaces.list",
            "List the ontology interfaces",
            "which ontology interfaces exist",
            InterfacesListParams,
        ),
        _read(
            "ontology.interfaces.implementers",
            "List the classes that implement one interface",
            "which classes implement this interface",
            ImplementersParams,
        ),
        _read(
            "ontology.objects.search",
            "Search objects of one type by field values",
            "find objects of this type",
            SearchParams,
        ),
        _read(
            "ontology.objects.search_around",
            "Read the objects linked to some objects",
            "what is linked to these objects",
            SearchAroundParams,
        ),
        _read(
            "ontology.objects.pivot",
            "Move from an object set across one link type",
            "pivot this object set",
            PivotParams,
        ),
        _read(
            "ontology.objects.aggregate",
            "Group objects of one type and aggregate fields",
            "count objects by field",
            AggregateParams,
        ),
        _read(
            "ontology.objects.get",
            "Read one object and its linked objects",
            "show this object",
            ObjectIdParams,
        ),
        _read(
            "ontology.object_sets.list",
            "List the saved object sets",
            "list my object sets",
            SetListParams,
            KV_READ,
        ),
        _write(
            "ontology.object_sets.save",
            "Save an object set definition",
            "save this object set",
            SetSaveParams,
            KV_WRITE,
        ),
        _write(
            "ontology.object_sets.delete",
            "Delete a saved object set",
            "delete this object set",
            SetNameParams,
            KV_WRITE,
        ),
        _read(
            "ontology.object_views.get",
            "Read the layout of an object type",
            "show the object view layout",
            ViewGetParams,
            KV_READ,
        ),
        _write(
            "ontology.object_views.put",
            "Save the layout of an object type",
            "save the object view layout",
            ViewPutParams,
            KV_WRITE,
        ),
        _write(
            "ontology.objects.edit",
            "Propose an edit to one object as a governed change",
            "edit this object",
            EditParams,
        ),
        _write(
            "ontology.objects.revert",
            "Revoke a proposed object edit",
            "revert this object edit",
            RevertParams,
        ),
        _write(
            "ontology.actions.apply",
            "Apply a named action to objects",
            "apply this action to these objects",
            ActionApplyParams,
            WRITE_SCOPE,
            Verb.ACT,
        ),
        _write(
            "ontology.document.process",
            "Chunk and embed one document node",
            "process this document",
            DocumentParams,
        ),
        _read(
            "ontology.actions.list",
            "List the actions that can be applied",
            "which actions can I apply",
            Params,
            scope="actions:read",
        ),
        unserved_write(
            module=m,
            op_id="ontology.functions.invoke",
            summary="Invoke a named compute function",
            example="invoke this function",
            params=FunctionParams,
            scope=WRITE_SCOPE,
        ),
        _read(
            "ontology.derive",
            "Preview a derived property",
            "preview this derived property",
            DeriveParams,
        ),
    )


specs = operations

__all__ = ["EngineReply", "handle_unserved", "operations", "specs"]
