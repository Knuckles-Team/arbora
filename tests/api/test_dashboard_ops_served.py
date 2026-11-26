"""Dashboards as catalog assets served over HTTP and MCP (ARBORA-OPS-R861)."""

from __future__ import annotations

from typing import Any

import pytest
from medulla import EngineResponseError

from tests.api._served_support import (
    McpAsk,
    capture_invocation,
    person,
    route_methods,
)
from tests.test_served_operation_api import _call, _value, served  # noqa: F401

ONE = {"Authorization": "Bearer one"}
TWO = {"Authorization": "Bearer two"}
KEY = "Idempotency-Key"


class DashboardEngine:
    """A recording ``DashboardAsset`` and ``CatalogRead`` engine with real rules."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.sent: list[tuple[str, str]] = []

    def _find(self, dashboard_id: str, principal: str) -> dict[str, Any]:
        row = self.store.get(dashboard_id)
        if row is None or self._role(row, principal) is None:
            raise EngineResponseError(
                "INVALID_ARGUMENT", "DASHBOARD_NOT_GRANTED: no grant covers this call"
            )
        return row

    @staticmethod
    def _role(row: dict[str, Any], principal: str) -> str | None:
        if row["owner"] == principal:
            return "owner"
        roles = {
            g["role"] for g in row["grants"] if g["principal_or_group"] == principal
        }
        return "edit" if "edit" in roles else "view" if roles else None

    @staticmethod
    def _receipt(row: dict[str, Any], body: bool = True) -> dict[str, Any]:
        out = {k: row[k] for k in ("id", "revision", "owner", "grants")}
        out["usage_count"] = row["usage_count"]
        if body:
            out["body"] = row["body"]
        return out

    def dashboard(self, params: dict[str, Any], principal: str) -> Any:
        op = params["op"]
        action = op["op"]
        self.sent.append((action, principal))
        if action == "save":
            row = self.store.get(op["id"])
            if row is None:
                if op["revision"] != 0:
                    raise EngineResponseError("DASHBOARD_REVISION_CONFLICT", "stale")
                row = self.store[op["id"]] = {
                    "id": op["id"],
                    "owner": principal,
                    "grants": [],
                    "revision": 0,
                    "usage_count": 0,
                }
            elif self._role(row, principal) in (None, "view"):
                raise EngineResponseError("DASHBOARD_NOT_GRANTED", "no grant")
            elif op["revision"] != row["revision"]:
                raise EngineResponseError(
                    "DASHBOARD_REVISION_CONFLICT", f"at revision {row['revision']}"
                )
            row["body"] = op["body"]
            row["revision"] += 1
            return self._receipt(row)
        row = self._find(op["id"], principal)
        if action == "get":
            row["usage_count"] += 1
            return self._receipt(row)
        if row["owner"] != principal:
            raise EngineResponseError("DASHBOARD_NOT_GRANTED", "owner only")
        if action == "share":
            row["grants"] = op["grants"]
            return self._receipt(row, body=False)
        del self.store[op["id"]]
        return {
            "id": op["id"],
            "revision": row["revision"],
            "owner": principal,
            "grants": [],
            "usage_count": 0,
        }

    def catalog(self, params: dict[str, Any], principal: str) -> Any:
        op = params["op"]
        assert op["op"] == "browse" and op["kind"] == "DashboardAsset"
        rows = [
            {
                "asset": f"dashboard:{r['id']}",
                "kind": "DashboardAsset",
                "name": r["body"]["layout"]["title"],
                "owner": r["owner"],
            }
            for r in self.store.values()
            if self._role(r, principal)
        ]
        return {"kind_counts": [], "rows": rows[: op["limit"]]}


DOC: dict[str, Any] = {
    "schema_version": 1,
    "id": "sales",
    "title": "Sales",
    "revision": 0,
    "params": [{"name": "region", "label": "Region", "type": "text", "value": "emea"}],
    "panels": [
        {
            "id": "p1",
            "title": "Revenue",
            "viz": "line",
            "query": {"kind": "uql", "text": "MATCH (n) RETURN n"},
            "layout": {"x": 0, "y": 0, "w": 6, "h": 3},
            "options": {"x": "month"},
        }
    ],
}


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Any:
    holder = capture_invocation(monkeypatch)
    return holder


@pytest.fixture
def env(stack: Any, served: Any, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: F811
    app, client = served
    engine = DashboardEngine()
    route_methods(
        client,
        {"DashboardAsset": engine.dashboard, "CatalogRead": engine.catalog},
        monkeypatch,
        {"one": person("one"), "two": person("two")},
    )
    return app, engine, stack["invocation"], monkeypatch


def _save(
    app: Any, document: dict[str, Any], expected: int | None, key: str, who=ONE
) -> Any:
    body: dict[str, Any] = {"document": document}
    if expected is not None:
        body["expected_revision"] = expected
    return _call(app, "dashboard.save", body, **who, **{KEY: key})


@pytest.mark.spec("ARBORA-OPS-R861.2")
def test_save_update_and_stale_revision_conflict(env: Any) -> None:
    app, engine, *_ = env
    created = _value(_save(app, DOC, 0, "k1"))
    assert created == {"id": "sales", "revision": 1, "owner": "person:one"}
    updated = _value(_save(app, {**DOC, "title": "Sales 2"}, 1, "k2"))
    assert updated["revision"] == 2
    stale = _save(app, DOC, 1, "k3")
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "DASHBOARD_REVISION_CONFLICT"
    assert engine.store["sales"]["revision"] == 2
    minted = _value(_save(app, {**DOC, "id": None}, None, "k4"))
    assert minted["revision"] == 1 and minted["id"] != "sales"
    assert _save(app, DOC, None, "k5").status_code == 400


PANEL_QUERIES: list[dict[str, Any]] = [
    {"kind": "uql", "text": "MATCH (n) RETURN n"},
    {"kind": "nl", "text": "how many open defects are there"},
    {"kind": "stored", "ref": "query:weekly"},
    {
        "kind": "fleet-call",
        "server": "gitlab-mcp",
        "tool": "list_pipelines",
        "arguments": {"project": "x", "limit": 5},
    },
    {"kind": "eg-op", "op": "eg.query.Uql", "params": {"text": "MATCH (n) RETURN n"}},
]


@pytest.mark.spec("ARBORA-OPS-R861.2")
def test_every_panel_kind_is_stored_as_data_and_runs_nothing(env: Any) -> None:
    app, engine, *_ = env
    panels = [
        {**DOC["panels"][0], "id": f"p{i}", "query": query}
        for i, query in enumerate(PANEL_QUERIES)
    ]
    _value(_save(app, {**DOC, "panels": panels}, 0, "kk"))
    # Saving is one engine write; no fleet call or query ran.
    assert engine.sent == [("save", "person:one")]
    got = _value(_call(app, "dashboard.get", {"id": "sales"}, **ONE))
    assert [p["query"] for p in got["document"]["panels"]] == PANEL_QUERIES
    bad = [
        {"kind": "fleet-call", "server": "x"},
        {"kind": "eg-op", "op": "eg.graph.DeleteNode", "params": {}},
        {"kind": "nl", "ref": "r"},
        {"kind": "shell", "text": "ls"},
    ]
    for query in bad:
        panel = {**DOC["panels"][0], "query": query}
        assert _save(app, {**DOC, "panels": [panel]}, 0, "kb").status_code == 400


@pytest.mark.spec("ARBORA-OPS-R861.4")
def test_list_result_rows_declare_the_title() -> None:
    from arbora.api.ops import dashboard

    assert "title" in dashboard.DashboardEntry.model_fields


@pytest.mark.spec("ARBORA-OPS-R861.3")
def test_validation_and_size_cap_make_no_engine_write(env: Any) -> None:
    app, engine, *_ = env
    no_query = {**DOC, "panels": [{**DOC["panels"][0], "query": {"kind": "uql"}}]}
    duplicate = {**DOC, "panels": [DOC["panels"][0], DOC["panels"][0]]}
    unknown = {**DOC, "surprise": 1}
    huge = {
        **DOC,
        "panels": [{**DOC["panels"][0], "options": {"blob": "x" * 1_100_000}}],
    }
    for bad in (no_query, duplicate, unknown, huge):
        assert _save(app, bad, 0, "kv").status_code in (400, 413)
    assert engine.sent == []


@pytest.mark.spec("ARBORA-OPS-R861.4")
def test_list_owned_and_shared_and_get_by_id(env: Any) -> None:
    app, engine, *_ = env
    _save(app, DOC, 0, "k1")
    got = _value(_call(app, "dashboard.get", {"id": "sales"}, **ONE))
    assert got["document"] == {
        **DOC,
        "revision": 1,
        "panels": [{**DOC["panels"][0], "terms": [], "concepts": []}],
    }
    assert got["role"] == "owner"
    assert engine.store["sales"]["usage_count"] == 1
    assert _value(_call(app, "dashboard.list", {}, **TWO))["items"] == []
    assert (
        _call(app, "dashboard.get", {"id": "sales"}, **TWO).json()["error"]["code"]
        == "DASHBOARD_NOT_GRANTED"
    )
    _value(
        _call(
            app,
            "dashboard.share",
            {"id": "sales", "grantee": "person:two", "role": "view", "action": "grant"},
            **ONE,
            **{KEY: "s1"},
        )
    )
    mine = _value(_call(app, "dashboard.list", {}, **ONE))["items"]
    theirs = _value(_call(app, "dashboard.list", {}, **TWO))["items"]
    assert mine == [
        {"id": "sales", "title": "Sales", "owner": "person:one", "role": "owner"}
    ]
    assert theirs[0]["role"] == "shared"
    assert _value(_call(app, "dashboard.get", {"id": "sales"}, **TWO))["role"] == "view"


@pytest.mark.spec("ARBORA-OPS-R861.5")
def test_delete_checks_revision_and_removes_the_asset(env: Any) -> None:
    app, engine, *_ = env
    _save(app, DOC, 0, "k1")
    stale = _call(
        app,
        "dashboard.delete",
        {"id": "sales", "expected_revision": 0},
        **ONE,
        **{KEY: "d1"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "DASHBOARD_REVISION_CONFLICT"
    other = _call(
        app,
        "dashboard.delete",
        {"id": "sales", "expected_revision": 1},
        **TWO,
        **{KEY: "d2"},
    )
    assert other.json()["error"]["code"] == "DASHBOARD_NOT_GRANTED"
    assert "sales" in engine.store
    gone = _value(
        _call(
            app,
            "dashboard.delete",
            {"id": "sales", "expected_revision": 1},
            **ONE,
            **{KEY: "d3"},
        )
    )
    assert gone == {"id": "sales", "deleted": True}
    assert "sales" not in engine.store


@pytest.mark.spec("ARBORA-OPS-R861.6", "ARBORA-OPS-R861.10")
def test_share_grant_revoke_and_view_caller_refused(env: Any) -> None:
    app, _engine, *_ = env
    _save(app, DOC, 0, "k1")
    share = {"id": "sales", "grantee": "person:two", "role": "edit"}
    granted = _value(
        _call(
            app, "dashboard.share", {**share, "action": "grant"}, **ONE, **{KEY: "s1"}
        )
    )
    assert granted["grants"] == [{"principal_or_group": "person:two", "role": "edit"}]
    access = _value(_call(app, "dashboard.access.get", {"id": "sales"}, **TWO))
    assert (
        access["role"] == "edit"
        and access["owner"] == "person:one"
        and len(access["grants"]) == 1
    )
    # An editor may not change grants: the engine lets only the owner.
    denied = _call(
        app, "dashboard.share", {**share, "action": "revoke"}, **TWO, **{KEY: "s2"}
    )
    assert denied.json()["error"]["code"] == "DASHBOARD_NOT_GRANTED"
    _value(
        _call(
            app,
            "dashboard.share",
            {**share, "role": "view", "action": "grant"},
            **ONE,
            **{KEY: "s3"},
        )
    )
    assert (
        _value(_call(app, "dashboard.access.get", {"id": "sales"}, **TWO))["role"]
        == "view"
    )
    assert (
        _value(_call(app, "dashboard.access.get", {"id": "sales"}, **TWO))["grants"]
        == []
    )
    revoked = _value(
        _call(
            app, "dashboard.share", {**share, "action": "revoke"}, **ONE, **{KEY: "s4"}
        )
    )
    assert revoked["grants"] == []
    assert _call(app, "dashboard.access.get", {"id": "sales"}, **TWO).status_code == 403
    both = {
        "id": "sales",
        "grantee": "a",
        "group": "b",
        "role": "view",
        "action": "grant",
    }
    assert _call(app, "dashboard.share", both, **ONE, **{KEY: "s5"}).status_code == 400


@pytest.mark.spec("ARBORA-OPS-R861.7")
def test_hidden_and_absent_dashboards_answer_alike(env: Any) -> None:
    app, *_ = env
    _save(app, DOC, 0, "k1")
    hidden = _call(app, "dashboard.get", {"id": "sales"}, **TWO)
    absent = _call(app, "dashboard.get", {"id": "nothing"}, **TWO)
    assert hidden.status_code == absent.status_code
    assert hidden.json()["error"]["code"] == absent.json()["error"]["code"]


@pytest.mark.spec("ARBORA-OPS-R861", "ARBORA-OPS-R861.8", "ARBORA-OPS-R861.9")
def test_save_share_list_conflict_delete_served(env: Any) -> None:
    app, engine, invocation, monkeypatch = env
    ask = McpAsk(invocation, monkeypatch)
    saved = ask(
        "act",
        "dashboard.save",
        {"document": DOC, "expected_revision": 0},
        execute=True,
        idempotency_key="m1",
    )
    assert saved["ok"] is True, saved
    _value(
        _call(
            app,
            "dashboard.share",
            {"id": "sales", "grantee": "person:two", "role": "view", "action": "grant"},
            **ONE,
            **{KEY: "s1"},
        )
    )
    assert [
        i["id"] for i in _value(_call(app, "dashboard.list", {}, **TWO))["items"]
    ] == ["sales"]
    assert (
        ask("ask", "dashboard.get", {"id": "sales"})["result"]["document"]["title"]
        == "Sales"
    )
    assert _save(app, DOC, 0, "k9").status_code == 409
    gone = ask(
        "act",
        "dashboard.delete",
        {"id": "sales", "expected_revision": 1},
        idempotency_key="m2",
    )
    assert gone["ok"] is True, gone
    assert "sales" not in engine.store


@pytest.mark.spec("ARBORA-OPS-R861.1")
def test_document_round_trip_and_rejects_unknown_field() -> None:
    from pydantic import ValidationError

    from arbora.api.ops import dashboard

    document = dashboard.DashboardDocument.model_validate(DOC)
    body = dashboard._body_of(document)
    rebuilt = dashboard._document_of(
        {
            "id": "sales",
            "revision": 3,
            "owner": "person:one",
            "grants": [],
            "body": body,
        }
    )
    assert rebuilt["revision"] == 3
    assert dashboard.DashboardDocument.model_validate(rebuilt).panels == document.panels
    with pytest.raises(ValidationError):
        dashboard.DashboardDocument.model_validate({**DOC, "extra": 1})


@pytest.mark.spec("ARBORA-OPS-R861.8")
def test_each_operation_reaches_its_handler() -> None:
    from arbora.api.ops import dashboard
    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.registry import Composite

    registry = get_registry()
    for op in dashboard.operations():
        assert registry.get(op.id) is not None
        assert isinstance(op.binding, Composite)
        module, _, name = op.binding.handler.rpartition(".")
        assert callable(getattr(dashboard, name)), module
