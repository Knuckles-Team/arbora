"""Ontology object sets, actions, edits and interfaces (ARBORA-OPS-R908, R922.1)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import ontology_objects as oo
from arbora.api.registry import Effect, Verb
from tests.test_served_operation_api import served  # noqa: F401

NO_OP: Any = None


def _context() -> Any:
    return SimpleNamespace(
        caller=SimpleNamespace(tenant="t", principal="p"), idempotency_key="k"
    )


class Engine:
    """Records engine sends and answers each method from a table."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], bool]] = []
        self.answers: dict[str, Any] = {}

    async def send(
        self, _c: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> Any:
        self.sent.append((method, params, idempotent))
        answer = self.answers.get(method, {})
        return answer(params) if callable(answer) else answer


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> Engine:
    fake = Engine()
    monkeypatch.setattr(oo, "engine_send", fake.send)
    return fake


class FakeKv:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], tuple[bytes, int]] = {}

    async def send(
        self, _c: Any, method: str, params: dict[str, Any], _key: Any
    ) -> Any:
        slot = (params["namespace"], params.get("key", ""))
        if method == "KvGet":
            row = self.rows.get(slot)
            return {} if row is None else {"value": row[0], "version": row[1]}
        if method == "KvCas":
            current = self.rows.get(slot, (b"", 0))[1]
            if current != params["expected_version"]:
                return {"swapped": False, "version": current}
            self.rows[slot] = (params["new"], current + 1)
            return {"swapped": True, "version": current + 1}
        if method == "KvDelete":
            self.rows.pop(slot)
            return {}
        if method == "KvScan":
            keys = sorted(k for (ns, k) in self.rows if ns == params["namespace"])
            return {
                "entries": [
                    {
                        "key": k,
                        "value": self.rows[(params["namespace"], k)][0],
                        "version": 1,
                    }
                    for k in keys
                ],
                "next_cursor": None,
            }
        raise AssertionError(method)


@pytest.fixture
def kv(monkeypatch: pytest.MonkeyPatch) -> FakeKv:
    fake = FakeKv()
    monkeypatch.setattr(oo, "_send", fake.send)
    return fake


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_search_compiles_to_uql_and_returns_objects(engine: Engine) -> None:
    engine.answers["Uql"] = {"Rows": {"rows": [{"id": "a"}, {"id": "b"}, {"id": "c"}]}}
    reply = await oo.handle_objects_search(
        _context(),
        {"object_type": "Person", "filter": {"name": "x'; DROP", "age": 3}, "limit": 2},
        NO_OP,
    )
    assert reply["value"]["objects"] == [{"id": "a"}, {"id": "b"}]
    assert reply["value"]["truncated"] is True and reply["value"]["next_cursor"] == "2"
    _method, request, _idem = engine.sent[0]
    # Values travel as bound parameters, never inside the statement text.
    assert request["text"] == (
        "MATCH (:Person) |> WHERE age = $p0 AND name = $p1"
        " |> ORDER BY id |> SKIP 0 |> LIMIT 3"
    )
    assert request["params"] == {"p0": {"Num": 3.0}, "p1": {"Str": "x'; DROP"}}
    with pytest.raises(ValidationError):
        oo.SearchParams(object_type="Person) |> DROP")
    around = await oo.handle_objects_search_around(
        _context(), {"object_ids": ["a"], "link_type": "cites", "hops": 2}, NO_OP
    )
    assert around["value"]["objects"]
    assert engine.sent[1][1]["text"] == (
        "MATCH () |> WHERE id IN $p0 |> TRAVERSE -[:cites]-{1,2}"
    )
    await oo.handle_objects_aggregate(
        _context(),
        {
            "object_type": "Person",
            "group_by": ["city"],
            "metrics": [{"op": "count"}, {"op": "sum", "field": "age"}],
        },
        NO_OP,
    )
    assert engine.sent[2][1]["text"] == (
        "MATCH (:Person) |> GROUP BY city AGGREGATE COUNT AS count, SUM(age) AS sum_age"
        " |> LIMIT 1000"
    )


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_object_sets_pivot_and_views_are_records_in_the_callers_session(
    engine: Engine, kv: FakeKv
) -> None:
    context = _context()
    saved = await oo.handle_object_sets_save(
        context,
        {
            "name": "adults",
            "definition": {"object_type": "Person", "filter": {"adult": True}},
        },
        NO_OP,
    )
    assert saved["value"] == {"id": "adults", "revision": 1}
    resaved = await oo.handle_object_sets_save(
        context, {"name": "adults", "definition": {"object_type": "Person"}}, NO_OP
    )
    assert resaved["value"]["revision"] == 2
    listed = await oo.handle_object_sets_list(context, {"limit": 10}, NO_OP)
    assert [s["id"] for s in listed["value"]["object_sets"]] == ["adults"]
    engine.answers["Uql"] = {"Rows": {"rows": [{"id": "n"}]}}
    await oo.handle_objects_pivot(
        context, {"object_set_id": "adults", "link_type": "knows"}, NO_OP
    )
    assert engine.sent[-1][1]["text"] == "MATCH (:Person) |> TRAVERSE -[:knows]-{1,1}"
    with pytest.raises(OperationRefused) as both:
        await oo.handle_objects_pivot(context, {"link_type": "knows"}, NO_OP)
    assert both.value.code == "INVALID_ARGUMENT"
    await oo.handle_object_sets_delete(context, {"name": "adults"}, NO_OP)
    with pytest.raises(OperationRefused) as gone:
        await oo.handle_object_sets_delete(context, {"name": "adults"}, NO_OP)
    assert gone.value.code == "NOT_FOUND"
    await oo.handle_object_views_put(
        context, {"object_type": "Person", "layout": {"a": 1}}, NO_OP
    )
    view = await oo.handle_object_views_get(context, {"object_type": "Person"}, NO_OP)
    assert view["value"]["layout"] == {"a": 1}


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_get_reads_the_object_and_an_absent_one_is_not_found(
    engine: Engine,
) -> None:
    engine.answers["Uql"] = lambda p: (
        {"Rows": {"rows": [{"id": "a"}]}}
        if "id = $p0" in p["text"] or "IN" in p["text"]
        else {}
    )
    got = await oo.handle_objects_get(_context(), {"object_id": "a"}, NO_OP)
    assert got["value"]["object"] == {"id": "a"}
    engine.answers["Uql"] = {"Rows": {"rows": []}}
    with pytest.raises(OperationRefused) as absent:
        await oo.handle_objects_get(_context(), {"object_id": "zz"}, NO_OP)
    assert absent.value.code == "NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_edit_and_revert_go_through_governed_change(
    engine: Engine, kv: FakeKv
) -> None:
    engine.answers["GovernedChange"] = {"state": "proposed"}
    params = {"object_id": "o1", "changes": {"name": "n"}, "reason": "typo"}
    first = await oo.handle_objects_edit(_context(), params, NO_OP)
    again = await oo.handle_objects_edit(_context(), params, NO_OP)
    assert first == again and first["value"]["state"] == "proposed"
    methods = [m for m, _p, _i in engine.sent]
    assert methods == ["GovernedChange", "GovernedChange"]  # never a direct write
    op = engine.sent[0][1]["op"]
    assert op["op"] == "propose" and op["request"]["target"] == "o1"
    assert op["request"]["kind"] == "object_edit"
    assert all(idem for _m, _p, idem in engine.sent)
    assert [k for (ns, k) in kv.rows if ns == oo.EDITS_NAMESPACE] == [
        first["value"]["change_id"]
    ]
    engine.answers["GovernedChange"] = {"state": "revoked"}
    reverted = await oo.handle_objects_revert(
        _context(), {"change_id": first["value"]["change_id"]}, NO_OP
    )
    assert reverted["value"]["state"] == "revoked"
    assert engine.sent[-1][1]["op"] == {
        "op": "revoke",
        "change_id": first["value"]["change_id"],
    }


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_apply_action_requires_plan_confirmation(engine: Engine) -> None:
    spec = next(o for o in oo.operations() if o.id == "ontology.actions.apply")
    # An act operation previews first and runs only on a confirmed plan.
    assert spec.verb is Verb.ACT and spec.effect is Effect.WRITE
    engine.answers["ActionSubmit"] = {"ok": True}
    reply = await oo.handle_actions_apply(
        _context(),
        {"action": "close", "object_ids": ["a", "b"], "params": {"why": "x"}},
        NO_OP,
    )
    assert [r["object_id"] for r in reply["value"]["results"]] == ["a", "b"]
    assert engine.sent[0][1] == {
        "action": "close",
        "params": {"why": "x", "object_id": "a"},
    }


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_document_process_returns_the_enrichment_as_the_job(
    engine: Engine,
) -> None:
    engine.answers["EnrichDocument"] = {"enrichment_id": "enr-1"}
    reply = await oo.handle_document_process(_context(), {"document_id": "d"}, NO_OP)
    assert reply["value"] == {"job_id": "enr-1"}
    engine.answers["EnrichDocument"] = {}
    with pytest.raises(OperationRefused) as odd:
        await oo.handle_document_process(_context(), {"document_id": "d"}, NO_OP)
    assert odd.value.code == "UNAVAILABLE"


@pytest.mark.spec("ARBORA-OPS-R908")
def test_the_registry_marks_functions_and_every_mark_is_a_read() -> None:
    from arbora.api.ops.registry_factory import get_registry
    from arbora.api.registry import Effect

    registry = get_registry()
    marked = [op for op in registry if op.function]
    assert marked and all(op.effect is Effect.READ for op in marked)
    assert registry["eg.compute.PageRank"].function
    assert not registry["memory.list"].function
    assert not registry["ontology.functions.invoke"].function
    invoke = registry["ontology.functions.invoke"]
    assert invoke.effect is Effect.READ and invoke.scopes == frozenset({"kg:read"})


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_function_invoke_runs_only_a_marked_operation_through_the_pipeline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora.api.ops.registry_factory import get_registry

    registry = get_registry()
    seen: list[tuple[str, dict[str, Any], bool]] = []

    async def pipeline(context: Any, op_id: str, args: Any, *, only: Any = None) -> Any:
        target = registry.get(op_id)
        seen.append((op_id, dict(args), bool(only(target)) if target else False))
        if target is None or not only(target):
            raise OperationRefused("NOT_FOUND")
        return {"ranked": []}

    monkeypatch.setattr(oo, "invoke_registered", pipeline)
    reply = await oo.handle_functions_invoke(
        _context(), {"function": "eg.compute.PageRank", "args": {"k": 1}}, NO_OP
    )
    assert reply == {"value": {"result": {"ranked": []}}}
    for refused_name in ("memory.list", "ontology.functions.invoke", "no.such"):
        with pytest.raises(OperationRefused) as refused:
            await oo.handle_functions_invoke(
                _context(), {"function": refused_name, "args": {}}, NO_OP
            )
        assert refused.value.code == "NOT_FOUND"
    assert seen[0] == ("eg.compute.PageRank", {"k": 1}, True)


@pytest.mark.spec("ARBORA-OPS-R908")
def test_function_invoke_is_served_under_the_function_scope(
    served: Any,  # noqa: F811
) -> None:
    from tests.test_served_operation_api import _call

    app, _client = served
    scope = _call(
        app,
        "ontology.functions.invoke",
        {"function": "eg.compute.GetBlastRadius", "args": {}},
    )
    # The caller holds kg:read, not the function's own compute scope.
    assert scope.json()["error"]["code"] == "SCOPE_REQUIRED", scope.text
    # A service-only function stays service-only behind its name.
    service = _call(
        app,
        "ontology.functions.invoke",
        {"function": "eg.compute.PageRank", "args": {}},
    )
    assert service.json()["error"]["code"] == "PRINCIPAL_NOT_ALLOWED", service.text
    unmarked = _call(
        app, "ontology.functions.invoke", {"function": "memory.list", "args": {}}
    )
    absent = _call(
        app, "ontology.functions.invoke", {"function": "no.such.fn", "args": {}}
    )
    assert (
        unmarked.json()["error"]["code"]
        == absent.json()["error"]["code"]
        == "NOT_FOUND"
    )


@pytest.mark.spec("ARBORA-OPS-R908")
async def test_actions_list_forwards_and_derive_runs_bounded_uql(
    engine: Engine,
) -> None:
    ops = {o.id: o for o in oo.operations()}
    assert ops["ontology.actions.list"].scopes == frozenset({"actions:read"})
    engine.answers["ActionList"] = {"actions": [{"name": "a", "params_schema": {}}]}
    listed = await oo.handle_actions_list(_context(), {}, NO_OP)
    assert listed["value"]["actions"][0]["name"] == "a"
    assert engine.sent[-1][:2] == ("ActionList", {})
    engine.answers["Uql"] = {"Rows": {"rows": [{"id": "x"}]}}
    derived = await oo.handle_derive(
        _context(),
        {"object_type": "Asset", "expression": "SELECT id", "limit": 5},
        NO_OP,
    )
    assert derived["value"] == {"preview": [{"id": "x"}], "limit": 5}
    method, request, idempotent = engine.sent[-1]
    assert (method, idempotent) == ("Uql", False)
    assert request["text"] == "MATCH (:Asset) |> SELECT id |> LIMIT 5"
    await oo.handle_derive(
        _context(),
        {"object_type": "Asset", "expression": "MATCH (:Asset) |> WHERE a = 1"},
        NO_OP,
    )
    assert engine.sent[-1][1]["text"].endswith("WHERE a = 1 |> LIMIT 50")
    with pytest.raises(ValidationError):
        oo.DeriveParams(object_type="Asset", expression="x", limit=500)


def _terms() -> list[dict[str, Any]]:
    return [
        {
            "iri": "urn:i:Asset",
            "local_name": "Asset",
            "interface": True,
            "implements": [],
        },
        {
            "iri": "urn:c:Server",
            "local_name": "Server",
            "interface": False,
            "implements": ["urn:i:Asset"],
        },
        {"iri": "urn:c:Doc", "local_name": "Doc", "interface": False, "implements": []},
    ]


@pytest.mark.spec("ARBORA-OPS-R922.1")
async def test_interfaces_and_implementers_read_the_engine_schema(
    engine: Engine,
) -> None:
    engine.answers["GraphSchemaClasses"] = {"terms": _terms(), "next_cursor": None}
    listed = await oo.handle_interfaces_list(_context(), {"limit": 50}, NO_OP)
    assert [i["name"] for i in listed["value"]["interfaces"]] == ["Asset"]
    assert listed["value"]["truncated"] is False
    found = await oo.handle_interfaces_implementers(
        _context(), {"name": "Asset", "limit": 50}, NO_OP
    )
    assert found["value"]["implementers"] == [
        {"class_name": "Server", "iri": "urn:c:Server"}
    ]
    with pytest.raises(OperationRefused) as unknown:
        await oo.handle_interfaces_implementers(
            _context(), {"name": "Doc", "limit": 5}, NO_OP
        )
    assert unknown.value.code == "INTERFACE_NOT_FOUND"


@pytest.mark.spec("ARBORA-OPS-R922.1")
async def test_interfaces_are_unavailable_until_the_engine_marks_class_rows(
    engine: Engine,
) -> None:
    engine.answers["GraphSchemaClasses"] = {
        "terms": [{"iri": "urn:c:Doc"}],
        "next_cursor": None,
    }
    for handler, params in (
        (oo.handle_interfaces_list, {"limit": 5}),
        (oo.handle_interfaces_implementers, {"name": "x", "limit": 5}),
    ):
        with pytest.raises(OperationRefused) as refused:
            await handler(_context(), params, NO_OP)
        assert refused.value.code == "UNAVAILABLE"
