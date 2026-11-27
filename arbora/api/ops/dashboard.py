"""Dashboards as catalog assets (ARBORA-OPS-R861).

``dashboard.save``, ``dashboard.get``, ``dashboard.list``, ``dashboard.delete``,
``dashboard.share`` and ``dashboard.access.get`` store, read and share one
dashboard document through the engine ``DashboardAsset`` method and list it
through ``CatalogRead``. Arbora holds no asset store: the engine owns the
owner, the grants, the revision check and the retention of a deleted body.

The engine body keeps typed panels (id, source, query, terms, concepts). The
rest of the document (title, visualization, layout, options, query kind) rides
in the body's opaque ``layout`` and the parameters in its opaque ``parameters``,
so a get returns exactly the document that was saved.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops._common import Params, build_read_op, build_write_op
from arbora.api.ops.engine_calls import engine_send, unavailable
from arbora.api.registry import (
    Effect,
    OpSpec,
)

#: The engine replays each dashboard write by its idempotency key.
NATIVE_IDEMPOTENCY: Mapping[str, str] = MappingProxyType(
    {
        "dashboard.save": "medulla:DashboardAsset",
        "dashboard.share": "medulla:DashboardAsset",
        "dashboard.delete": "medulla:DashboardAsset",
    }
)
READ_SCOPE = "kg:read"
WRITE_SCOPE = "kg:write"
#: The catalog kind the engine gives a dashboard asset.
DASHBOARD_KIND = "DashboardAsset"
#: Largest document Arbora forwards; the engine knob may refuse a smaller one.
MAX_DOCUMENT_BYTES = 1_048_576

_ID = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_NAME = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
Role = Literal["owner", "edit", "view"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class UqlQuery(_Frozen):
    kind: Literal["uql"]
    text: str = Field(min_length=1, max_length=2000)


class NlQuery(_Frozen):
    kind: Literal["nl"]
    text: str = Field(min_length=1, max_length=2000)


class StoredQuery(_Frozen):
    kind: Literal["stored"]
    ref: str = Field(min_length=1, max_length=256)


class FleetCallQuery(_Frozen):
    """One fleet tool call, stored as data; saving runs nothing."""

    kind: Literal["fleet-call"]
    server: str = Field(pattern=_NAME)
    tool: str = Field(pattern=_NAME)
    arguments: dict[str, Any] = Field(default_factory=dict)


class EngineOpQuery(_Frozen):
    """One named engine query operation, stored as data; saving runs nothing."""

    kind: Literal["eg-op"]
    op: str = Field(pattern=r"^eg\.query\.[A-Za-z][A-Za-z0-9]{0,63}$")
    params: dict[str, Any] = Field(default_factory=dict)


#: Every panel query kind. The text kinds keep their text and the structured
#: kinds keep their fields; none of them runs at save time.
DashboardQuery = Annotated[
    UqlQuery | NlQuery | StoredQuery | FleetCallQuery | EngineOpQuery,
    Field(discriminator="kind"),
]
_QUERY = TypeAdapter(DashboardQuery)


class DashboardPanel(_Frozen):
    id: str = Field(pattern=_ID)
    title: str = Field(min_length=1, max_length=120)
    viz: str = Field(min_length=1, max_length=32)
    query: DashboardQuery
    layout: dict[str, int] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)
    terms: list[str] = Field(default_factory=list, max_length=64)
    concepts: list[str] = Field(default_factory=list, max_length=64)


class DashboardParam(_Frozen):
    name: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    type: Literal["text", "number"]
    value: str | int | float


class DashboardDocument(_Frozen):
    """The document a client edits; ``id`` is minted on a first save without one."""

    schema_version: Literal[1]
    id: str | None = Field(default=None, pattern=_ID)
    title: str = Field(min_length=1, max_length=200)
    revision: int = Field(default=0, ge=0)
    params: list[DashboardParam] = Field(default_factory=list, max_length=32)
    panels: list[DashboardPanel] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def _unique_panels(self) -> DashboardDocument:
        ids = [panel.id for panel in self.panels]
        if len(set(ids)) != len(ids):
            raise ValueError("panel ids must be unique")
        return self


class DashboardSaveParams(Params):
    document: DashboardDocument
    expected_revision: int | None = Field(default=None, ge=0)


class DashboardRefParams(Params):
    id: str = Field(pattern=_ID)


class DashboardDeleteParams(DashboardRefParams):
    expected_revision: int = Field(ge=0)


class DashboardListParams(Params):
    limit: int = Field(default=50, ge=1, le=200)
    cursor: str | None = Field(default=None, min_length=1, max_length=512)


class DashboardShareParams(DashboardRefParams):
    grantee: str | None = Field(default=None, min_length=1, max_length=256)
    group: str | None = Field(default=None, min_length=1, max_length=256)
    role: Literal["view", "edit"]
    action: Literal["grant", "revoke"]

    @model_validator(mode="after")
    def _one_subject(self) -> DashboardShareParams:
        if (self.grantee is None) == (self.group is None):
            raise ValueError("name one grantee or one group")
        return self


class Grant(Params):
    principal_or_group: str
    role: str


class DashboardSaveResult(Params):
    id: str
    revision: int
    owner: str


class DashboardGetResult(Params):
    document: dict[str, Any]
    owner: str
    role: Role


class DashboardEntry(Params):
    id: str
    title: str | None = None
    owner: str | None = None
    role: Literal["owner", "shared"]


class DashboardListResult(Params):
    items: list[DashboardEntry]
    next_cursor: str | None = None


class DashboardDeleteResult(Params):
    id: str
    deleted: bool


class DashboardShareResult(Params):
    id: str
    grants: list[Grant]


class DashboardAccessResult(Params):
    id: str
    owner: str
    role: Role
    grants: list[Grant]


def _source_of(query: Any) -> str:
    """The channel a panel reads: its language, or the stored query or operation itself."""
    if query.kind == "stored":
        return str(query.ref)
    if query.kind == "fleet-call":
        return f"fleet:{query.server}.{query.tool}"
    if query.kind == "eg-op":
        return str(query.op)
    return str(query.kind)


def _query_text(query: Any) -> str:
    """The engine's query string: text, the stored reference, or the structured fields as JSON."""
    if query.kind in ("uql", "nl"):
        return str(query.text)
    if query.kind == "stored":
        return str(query.ref)
    return json.dumps(
        query.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    )


def _query_of(kind: str, text: str) -> dict[str, Any]:
    """The query a stored panel row holds, rebuilt as its kind declares."""
    if kind in ("uql", "nl"):
        return {"kind": kind, "text": text}
    if kind == "stored":
        return {"kind": kind, "ref": text}
    return dict(_QUERY.validate_python(json.loads(text)).model_dump(mode="json"))


def _body_of(document: DashboardDocument) -> dict[str, Any]:
    """The engine body of a document; everything the engine does not type rides opaque."""
    return {
        "panels": [
            {
                "id": panel.id,
                "source": _source_of(panel.query),
                "query": _query_text(panel.query),
                "terms": panel.terms,
                "concepts": panel.concepts,
            }
            for panel in document.panels
        ],
        "layout": {
            "schema_version": 1,
            "title": document.title,
            "panels": {
                panel.id: {
                    "title": panel.title,
                    "viz": panel.viz,
                    "layout": panel.layout,
                    "options": panel.options,
                    "query_kind": panel.query.kind,
                }
                for panel in document.panels
            },
        },
        "parameters": [param.model_dump(mode="json") for param in document.params],
    }


def _document_of(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild the saved document from an engine receipt's body."""
    try:
        body = receipt["body"]
        meta = body["layout"]["panels"]
        panels = []
        for row in body["panels"]:
            extra = meta[row["id"]]
            kind = extra["query_kind"]
            query = _query_of(kind, row["query"])
            panels.append(
                {
                    "id": row["id"],
                    "title": extra["title"],
                    "viz": extra["viz"],
                    "query": query,
                    "layout": extra["layout"],
                    "options": extra["options"],
                    "terms": row.get("terms", []),
                    "concepts": row.get("concepts", []),
                }
            )
        return {
            "schema_version": 1,
            "id": receipt["id"],
            "title": body["layout"]["title"],
            "revision": receipt["revision"],
            "params": body.get("parameters") or [],
            "panels": panels,
        }
    except (KeyError, TypeError, ValueError):
        raise unavailable("dashboard body is not a dashboard document") from None


def _groups(context: Any) -> set[str]:
    """Group names the caller's verified actor carries (roles name groups too)."""
    actor = getattr(getattr(context.caller, "session", None), "actor", None)
    names: set[str] = set()
    for attribute in ("groups", "roles"):
        names.update(str(name) for name in getattr(actor, attribute, ()) or ())
    return names


def _role_of(context: Any, receipt: Mapping[str, Any]) -> Role:
    """The caller's own role: owner, else the strongest grant naming it or a group."""
    principal = str(context.caller.principal)
    if receipt.get("owner") == principal:
        return "owner"
    names = {principal} | _groups(context)
    roles = {
        grant.get("role")
        for grant in receipt.get("grants", [])
        if grant.get("principal_or_group") in names
    }
    if "edit" in roles:
        return "edit"
    if "view" in roles:
        return "view"
    # The engine admitted the read, so a grant covers the caller; its holder is
    # a group this verified identity does not enumerate.
    raise unavailable("caller role is not derivable from the stored grants")


async def _dashboard(
    context: Any, action: dict[str, Any], *, keyed: bool = False
) -> Mapping[str, Any]:
    result = await engine_send(
        context, "DashboardAsset", {"op": action}, idempotent=keyed
    )
    if not isinstance(result, Mapping):
        raise unavailable("engine result malformed")
    return result


def _grants(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        return [
            {"principal_or_group": g["principal_or_group"], "role": g["role"]}
            for g in receipt["grants"]
        ]
    except (KeyError, TypeError):
        raise unavailable("engine result malformed") from None


async def handle_dashboard_save(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Save one document; a stale expected revision is the engine's conflict."""
    document = DashboardDocument.model_validate(params["document"])
    body = _body_of(document)
    if len(document.model_dump_json()) > MAX_DOCUMENT_BYTES:
        raise OperationRefused(
            "INVALID_ARGUMENT", {"field": "document", "reason": "payload_too_large"}
        )
    dashboard_id = document.id or uuid.uuid4().hex
    expected = params.get("expected_revision")
    if document.id is not None and expected is None:
        raise OperationRefused("INVALID_ARGUMENT", {"field": "expected_revision"})
    receipt = await _dashboard(
        context,
        {"op": "save", "id": dashboard_id, "body": body, "revision": expected or 0},
        keyed=True,
    )
    try:
        return {
            "id": receipt["id"],
            "revision": receipt["revision"],
            "owner": receipt["owner"],
        }
    except KeyError:
        raise unavailable("engine result malformed") from None


async def handle_dashboard_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """One document for its owner or a grantee; the engine counts the view."""
    receipt = await _dashboard(context, {"op": "get", "id": params["id"]})
    return {
        "document": _document_of(receipt),
        "owner": receipt["owner"],
        "role": _role_of(context, receipt),
    }


async def handle_dashboard_list(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The dashboards the caller owns or may read, paged by the engine catalog."""
    action: dict[str, Any] = {
        "op": "browse",
        "kind": DASHBOARD_KIND,
        "limit": params["limit"],
    }
    if params.get("cursor") is not None:
        action["cursor"] = params["cursor"]
    answer = await engine_send(context, "CatalogRead", {"op": action})
    rows = answer.get("rows") if isinstance(answer, Mapping) else None
    if not isinstance(rows, list):
        raise unavailable("engine result malformed")
    principal = str(context.caller.principal)
    items = []
    for row in rows:
        try:
            identifier = str(row["asset"]).removeprefix("dashboard:")
        except (KeyError, TypeError):
            raise unavailable("engine result malformed") from None
        items.append(
            {
                "id": identifier,
                "title": row.get("name"),
                "owner": row.get("owner"),
                "role": "owner" if row.get("owner") == principal else "shared",
            }
        )
    return {"items": items, "next_cursor": answer.get("next_cursor")}


async def handle_dashboard_delete(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Soft delete at the revision the caller read; the engine checks it and keeps the body."""
    await _dashboard(
        context,
        {
            "op": "delete",
            "id": params["id"],
            "expected_revision": params["expected_revision"],
        },
        keyed=True,
    )
    return {"id": params["id"], "deleted": True}


async def handle_dashboard_share(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """Grant or revoke one role; the engine changes that one grant and only for the owner."""
    subject = params.get("grantee") or params["group"]
    if params["action"] == "grant":
        action = {
            "op": "grant",
            "id": params["id"],
            "grant": {"principal_or_group": subject, "role": params["role"]},
        }
    else:
        action = {"op": "revoke", "id": params["id"], "principal_or_group": subject}
    receipt = await _dashboard(context, action, keyed=True)
    return {"id": params["id"], "grants": _grants(receipt)}


async def handle_dashboard_access_get(
    context: Any, params: Mapping[str, Any], op: OpSpec
) -> dict[str, Any]:
    """The caller's role and, for an owner or editor, the grants."""
    receipt = await _dashboard(context, {"op": "get", "id": params["id"]})
    role = _role_of(context, receipt)
    return {
        "id": params["id"],
        "owner": receipt["owner"],
        "role": role,
        "grants": _grants(receipt) if role in ("owner", "edit") else [],
    }


def operations() -> tuple[OpSpec, ...]:
    module = "arbora.api.ops.dashboard"
    return (
        build_write_op(
            op_id="dashboard.save",
            summary="Save a dashboard document as a catalog asset",
            examples=("save this dashboard",),
            params=DashboardSaveParams,
            result=DashboardSaveResult,
            handler=f"{module}.handle_dashboard_save",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="dashboard.get",
            summary="Load one dashboard document with the caller's role",
            examples=("open this dashboard",),
            params=DashboardRefParams,
            result=DashboardGetResult,
            handler=f"{module}.handle_dashboard_get",
            scope=READ_SCOPE,
        ),
        build_read_op(
            op_id="dashboard.list",
            summary="List the dashboards the caller owns or may read",
            examples=("list my dashboards",),
            params=DashboardListParams,
            result=DashboardListResult,
            handler=f"{module}.handle_dashboard_list",
            scope=READ_SCOPE,
        ),
        build_write_op(
            op_id="dashboard.delete",
            effect=Effect.DESTRUCTIVE,
            summary="Delete a dashboard; the engine keeps the body for its retention",
            examples=("delete this dashboard",),
            params=DashboardDeleteParams,
            result=DashboardDeleteResult,
            handler=f"{module}.handle_dashboard_delete",
            scope=WRITE_SCOPE,
        ),
        build_write_op(
            op_id="dashboard.share",
            summary="Grant or revoke view or edit access to a dashboard",
            examples=("share this dashboard with a colleague",),
            params=DashboardShareParams,
            result=DashboardShareResult,
            handler=f"{module}.handle_dashboard_share",
            scope=WRITE_SCOPE,
        ),
        build_read_op(
            op_id="dashboard.access.get",
            summary="Read the caller's role on a dashboard and its grants",
            examples=("who can edit this dashboard",),
            params=DashboardRefParams,
            result=DashboardAccessResult,
            handler=f"{module}.handle_dashboard_access_get",
            scope=READ_SCOPE,
        ),
    )


specs = operations

__all__ = [
    "NATIVE_IDEMPOTENCY",
    "DashboardDocument",
    "handle_dashboard_access_get",
    "handle_dashboard_delete",
    "handle_dashboard_get",
    "handle_dashboard_list",
    "handle_dashboard_save",
    "handle_dashboard_share",
    "operations",
    "specs",
]
