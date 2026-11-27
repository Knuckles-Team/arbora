"""Paper-shortfall maintenance: sealed verdict, zero-effect refusals, one governed purge (ARBORA-OPS-R072)."""

from __future__ import annotations

import json

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import maintenance
from tests.api._lane_double import EngineDouble, context

_PARAMS = {
    "owner_authorization_ref": "auth-1",
    "expected_revision": 3,
    "cost_ceiling": 500,
    "admission_verdict_ref": "verdict-1",
    "purge_ids": ["p1", "p2"],
}


def _score(name: str) -> dict:
    return {"option_id": name, "score": {"value": 1}}


def _candidate(item_id: str) -> dict:
    """One engine ``PurgeCandidate`` (MEDULLA-DECISION-ENGINE-R826): no cost field."""
    return {
        "item_id": item_id,
        "terminal": True,
        "leased": False,
        "authoritative": False,
        "source_receipt": True,
        "decision_receipt": True,
        "last_source_copy": False,
        "active_work": False,
    }


def _verdict(**changes: object) -> dict:
    record = {
        "question_kind": "ingest_shortfall",
        "outcome": {
            "outcome": "advisory",
            "scores": [_score("goal:g1"), _score("purge:p1"), _score("purge:p2")],
        },
        "params": [
            {
                "name": "goals",
                "value": {
                    "type": "text",
                    "value": json.dumps(
                        [
                            {"goal_id": "g1", "cost": 450, "expected_papers": 9},
                            {"goal_id": "g2", "cost": 900, "expected_papers": 1},
                        ]
                    ),
                },
            },
            {
                "name": "purge_candidates",
                "value": {
                    "type": "text",
                    "value": json.dumps(
                        [_candidate("p1"), _candidate("p2"), _candidate("p3")]
                    ),
                },
            },
        ],
    }
    return {**record, **changes}


def _serve(monkeypatch: pytest.MonkeyPatch, client: EngineDouble):
    import medulla.generated as generated

    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {maintenance.METHOD, "DecisionLog"},
    )

    async def send(client, method, params, graph, **kw):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)
    return context(client, monkeypatch)


async def _refused(ctx, params: dict) -> OperationRefused:
    with pytest.raises(OperationRefused) as caught:
        await maintenance.handle_paper_shortfall_run(ctx, params, None)  # type: ignore[arg-type]
    return caught.value


@pytest.mark.spec("ARBORA-OPS-R072")
async def test_admitted_purge_is_one_revision_guarded_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineDouble(
        DecisionLog=_verdict(), ApplyChangeEnvelopeDraft={"status": "applied"}
    )
    ctx = _serve(monkeypatch, client)
    answer = await maintenance.handle_paper_shortfall_run(ctx, _PARAMS, None)  # type: ignore[arg-type]
    assert answer["value"]["purged"] == ["p1", "p2"] and answer["value"]["cost"] == 450
    applied = [s for s in client.sent if s[0] == "ApplyChangeEnvelopeDraft"]
    ((_, params, _, key),) = applied
    mutation = params["draft"]["mutation"]
    assert key == "key-1" and mutation["expected_graph_version"] == 3
    assert [o["method"]["params"]["node_id"] for o in mutation["operations"]] == [
        "p1",
        "p2",
    ]


@pytest.mark.spec("ARBORA-OPS-R072")
async def test_pre_effect_refusals_send_no_purge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = EngineDouble(DecisionLog=_verdict())
    ctx = _serve(monkeypatch, client)
    assert (
        await _refused(ctx, {**_PARAMS, "owner_authorization_ref": None})
    ).code == "SCOPE_REQUIRED"
    over = await _refused(ctx, {**_PARAMS, "cost_ceiling": 449})
    assert (
        over.code == "POLICY_DENIED" and over.details["reason"] == "cost_over_ceiling"
    )
    unlisted = await _refused(ctx, {**_PARAMS, "purge_ids": ["p3"]})
    assert unlisted.details["reason"] == "purge_not_admitted"
    assert [s[0] for s in client.sent if s[0] == "ApplyChangeEnvelopeDraft"] == []


@pytest.mark.spec("ARBORA-OPS-R072")
async def test_a_foreign_verdict_and_a_stale_revision_purge_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = EngineDouble(DecisionLog=_verdict(question_kind="task_plan"))
    refused = await _refused(_serve(monkeypatch, other), _PARAMS)
    assert refused.code == "RECEIPT_MISMATCH"
    assert [s[0] for s in other.sent] == ["DecisionLog"]
    stale = EngineDouble(
        DecisionLog=_verdict(),
        ApplyChangeEnvelopeDraft=EngineRefusal("GRAPH_REVISION_STALE"),
    )
    ctx = _serve(monkeypatch, stale)
    with pytest.raises(EngineRefusal) as caught:
        await maintenance.handle_paper_shortfall_run(ctx, _PARAMS, None)  # type: ignore[arg-type]
    assert caught.value.code == "GRAPH_REVISION_STALE"


@pytest.mark.spec("ARBORA-OPS-R072")
async def test_the_missing_method_refuses_naming_it_before_any_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import medulla.generated as generated

    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) - {maintenance.METHOD},
    )
    client = EngineDouble()
    missing = await _refused(context(client, monkeypatch), _PARAMS)
    assert (
        missing.code == "UNAVAILABLE"
        and missing.details["missing"] == maintenance.METHOD
    )
    assert client.sent == []
