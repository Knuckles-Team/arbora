"""External policy sync: bundle decoding, per-target rendering and the fail-closed applier."""

from __future__ import annotations

import json
import re

import pytest

from graph_os.access import external_policy_sync as sync

TABLE = sync.TrinoTargetTable(
    "lakehouse", "analytics", "docs", has_markings_column=True
)


def predicate(name: str) -> str:
    role = f"{sync.MARKING_ROLE_PREFIX}{name}"
    return json.dumps(
        {"kind": "requires_role", "role": role, "column": sync.RESERVED_MARKING_COLUMN}
    )


def bundle(
    *,
    governs=("M1",),
    markings=("confidential",),
    subject="svc:restricted",
    roles=("kg:read",),
):
    return sync.PolicyBundle.from_json(
        {
            "version": "policy-bundle-v1",
            "generated_from": "epoch:sha256:deadbeef",
            "governs": list(governs),
            "tenant": "tenant-a",
            "graphs": ["tenant:tenant-a"],
            "caller": {"subject": subject, "effective_roles": list(roles)},
            "markings": {m: {"predicate": predicate(m)} for m in markings},
        }
    )


def cleared(**kw):
    kw.update(subject="svc:cleared", roles=("kg:read", "marking:confidential"))
    return bundle(**kw)


def reasons(rendered):
    return " | ".join(rendered.not_applicable)


@pytest.fixture(autouse=True)
def enabled(monkeypatch):
    monkeypatch.setenv("EXTERNAL_POLICY_SYNC_ENABLED", "true")


@pytest.mark.parametrize(
    ("governs", "ok"),
    [(["M1"], True), ([], False), (["M1", "M7"], False), (["M6"], False)],
)
def test_governs_recognition(governs, ok):
    assert bundle(governs=governs).governs_recognized() is ok


def test_decode_and_refusals():
    b = bundle()
    assert b.caller.holds("kg:read") and not b.caller.holds("marking:confidential")
    assert b.markings["confidential"].decode().marking_name == "confidential"
    assert not hasattr(b, "principals")
    old_shape = {
        "version": "v",
        "generated_from": "x",
        "governs": ["M1"],
        "tenant": "t",
        "graphs": [],
        "principals": {"a": []},
        "markings": {},
    }
    for raw in (old_shape, {"version": "v"}):
        with pytest.raises(ValueError):
            sync.PolicyBundle.from_json(raw)
    unknown = sync.PolicyBundle.from_json(
        {
            **old_shape,
            "principals": None,
            "caller": {"subject": "s", "effective_roles": []},
            "markings": {"x": {"predicate": json.dumps({"kind": "row_content_match"})}},
        }
    )
    with pytest.raises(ValueError):
        unknown.markings["x"].decode()


def test_trino_filters_only_columned_tables_and_never_a_catch_all():
    gap = sync.TrinoTargetTable(
        "lakehouse", "analytics", "no_column", has_markings_column=False
    )
    rendered = sync.TrinoRenderer(tables=(TABLE, gap)).render(
        bundle(markings=("confidential", "restricted"))
    )
    assert "no_column" in reasons(rendered)
    assert "single-caller bundle" in reasons(rendered)
    assert {r["table"] for r in rendered.payload} == {"docs"}
    for row in rendered.payload:
        assert row["user"] == "svc:restricted"
        assert re.fullmatch(row["user"], "someone:else") is None
        assert row["filter"].startswith(f"NOT contains({sync.RESERVED_MARKING_COLUMN}")
    assert {
        m
        for m in ("confidential", "restricted")
        for r in rendered.payload
        if f"'{m}'" in r["filter"]
    } == {
        "confidential",
        "restricted",
    }


def test_trino_cleared_caller_gets_no_rule_and_subject_is_escaped():
    assert sync.TrinoRenderer(tables=(TABLE,)).render(cleared()).payload == []
    row = (
        sync.TrinoRenderer(tables=(TABLE,))
        .render(bundle(subject="svc.planner+eu"))
        .payload[0]
    )
    assert re.fullmatch(row["user"], "svc.planner+eu")
    assert re.fullmatch(row["user"], "svcxplanner+eu") is None


def test_renderers_refuse_unrecognized_governs():
    with pytest.raises(ValueError):
        sync.TrinoRenderer(tables=()).render(bundle(governs=["M7"]))


def test_opensearch_dls_roles():
    renderer = sync.OpenSearchRenderer(index_patterns=("kg-*",))
    restricted, free = (
        renderer.render(bundle()).payload[0],
        renderer.render(cleared()).payload[0],
    )
    assert restricted["dls_query"] == {
        "bool": {"must_not": [{"term": {"marking": "confidential"}}]}
    }
    assert free["dls_query"] == {"match_all": {}}
    assert restricted["index_pattern"] == "kg-*"


def test_lakekeeper_tuples_only_for_table_scoped_markings():
    assert sync.LakekeeperRenderer(table_scope={}).render(bundle()).payload == []
    ref = sync.LakekeeperTableRef(namespace="analytics", table="t")
    scope = {"confidential": ref}
    assert sync.LakekeeperRenderer(table_scope=scope).render(bundle()).payload == [
        {"namespace": "analytics", "table": "t", "openfga_tuples": []}
    ]
    entry = sync.LakekeeperRenderer(table_scope=scope).render(cleared()).payload[0]
    assert {t["user"] for t in entry["openfga_tuples"]} == {"oidc~svc:cleared"}


def applier():
    renderers = {
        "trino": sync.TrinoRenderer(tables=(TABLE,)),
        "opensearch": sync.OpenSearchRenderer(index_patterns=("kg-*",)),
        "lakekeeper": sync.LakekeeperRenderer(table_scope={}),
    }
    clients = {n: sync.InMemoryTargetClient(name=n) for n in renderers}
    return sync.Applier(renderers, clients), clients


def fetched(b, error=None):
    return sync.FetchedBundle(bundle=b, fetched_at_monotonic=0.0, error=error)


def test_applier_disabled_by_default(monkeypatch):
    monkeypatch.delenv("EXTERNAL_POLICY_SYNC_ENABLED")
    a, clients = applier()
    report = a.apply_all(fetched(bundle()))
    assert report["disabled"] and report["outcomes"] == []
    assert all(c.applied == [] and c.deny_all_calls == 0 for c in clients.values())


@pytest.mark.parametrize(
    "bad", [fetched(None, "fetch failed"), fetched(bundle(governs=["M6"]))]
)
def test_applier_denies_all_when_bundle_unusable(bad):
    a, clients = applier()
    outcomes = a.apply_all(bad)["outcomes"]
    assert {o["action"] for o in outcomes} == {"denied-all"}
    assert all(c.deny_all_calls == 1 and c.applied == [] for c in clients.values())


def test_applier_is_idempotent_and_reapplies_on_change():
    a, clients = applier()
    assert {o["action"] for o in a.apply_all(fetched(bundle()))["outcomes"]} == {
        "applied"
    }
    assert {o["action"] for o in a.apply_all(fetched(bundle()))["outcomes"]} == {
        "skipped-unchanged"
    }
    assert all(len(c.applied) == 1 for c in clients.values())
    third = {
        o["target"]: o["action"]
        for o in a.apply_all(fetched(bundle(markings=("restricted",))))["outcomes"]
    }
    assert third == {
        "trino": "applied",
        "opensearch": "applied",
        "lakekeeper": "skipped-unchanged",
    }


def test_applier_rejects_mismatched_names():
    with pytest.raises(ValueError):
        sync.Applier(
            {"trino": sync.TrinoRenderer(tables=())},
            {"opensearch": sync.InMemoryTargetClient("opensearch")},
        )


def test_dirty_tenant_bookkeeping_and_flag(monkeypatch):
    sync.invalidate_cache("", "n")
    assert not sync.is_dirty("")
    sync.invalidate_cache("tenant-x", "n")
    assert sync.is_dirty("tenant-x")
    sync.clear_dirty_state("tenant-x")
    assert not sync.is_dirty("tenant-x")
    monkeypatch.delenv("EXTERNAL_POLICY_SYNC_ENABLED")
    assert sync.external_policy_sync_enabled() is False
