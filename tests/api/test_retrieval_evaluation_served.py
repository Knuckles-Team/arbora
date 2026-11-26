"""Retrieval evaluation and authorized context search forwarding (ARBORA-OPS-R063)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arbora.api.invoke.pipeline import OperationRefused
from arbora.api.ops import retrieval
from arbora.api.registry import Effect
from tests.api._lane_double import EngineDouble, context

OPS = {op.id: op for op in retrieval.operations()}
PIN = {"id": "c", "revision": 1, "digest": "cd"}


def _run(algorithm: str, ranked: dict[str, list[str]], digest: str = "cd") -> dict:
    return {
        "algorithm_id": algorithm,
        "version": "1",
        "settings_digest": f"s-{algorithm}",
        "corpus_digest": digest,
        "input_digest": "in",
        "rankings": [{"query_id": q, "document_ids": d} for q, d in ranked.items()],
    }


def _params(**over: Any) -> dict[str, Any]:
    base = {
        "corpus": PIN,
        "labels": {"id": "l", "revision": 1, "digest": "ld"},
        "label_pairs": [
            {"query_id": "q1", "document_id": "d1"},
            {"query_id": "q2", "document_id": "d2"},
        ],
        "queries": {"id": "qs", "revision": 1, "digest": "qd"},
        "query_ids": ["q1", "q2"],
        "k": 1,
        "evaluator_revision": 1,
        "algorithms": [
            _run("bm25", {"q1": ["d1"], "q2": ["d9"]}),
            _run("dense", {"q1": ["d1"], "q2": ["d2"]}),
        ],
    }
    return {**base, **over}


def _serve(monkeypatch: pytest.MonkeyPatch, *methods: str) -> None:
    import medulla.generated as generated

    from arbora.pin_lag import medulla as pin_lag

    monkeypatch.setattr(pin_lag, "require_models", lambda _names: None)
    monkeypatch.setattr(pin_lag, "require_methods", lambda *_names: None)
    monkeypatch.setattr(
        generated,
        "METHOD_IDS",
        frozenset(generated.METHOD_IDS) | {"DecisionEval", *methods},
    )

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any):
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)


def _ledger(params: dict[str, Any]) -> dict[str, int]:
    """An independent hits@k count, the way an evaluator would compute it."""
    relevant: dict[str, set[str]] = {}
    for pair in params["label_pairs"]:
        relevant.setdefault(pair["query_id"], set()).add(pair["document_id"])
    return {
        run["algorithm_id"]: sum(
            1
            for row in run["rankings"]
            if relevant[row["query_id"]] & set(row["document_ids"][: params["k"]])
        )
        for run in params["algorithms"]
    }


def _engine_scores(request: Any) -> dict[str, Any]:
    body = request["op"]["request"]
    counts = _ledger(
        {
            "label_pairs": body["labels"]["pairs"],
            "k": body["k"],
            "algorithms": body["algorithms"],
        }
    )
    return {
        "scores": [
            {
                "algorithm_id": name,
                "numerator": hits,
                "denominator": len(body["queries"]["query_ids"]),
            }
            for name, hits in counts.items()
        ]
    }


@pytest.mark.spec("ARBORA-OPS-R063.1")
def test_models_round_trip_and_bound_the_inputs() -> None:
    parsed = retrieval.RetrievalCompareParams(**_params())
    assert parsed.k == 1 and len(parsed.algorithms) == 2
    for bad in (
        _params(k=0),
        _params(k=1001),
        _params(algorithms=[_run("only", {"q1": ["d1"]})]),
        _params(extra_field=1),
    ):
        with pytest.raises(ValidationError):
            retrieval.RetrievalCompareParams(**bad)
    for bad_ctx in ({"query": ""}, {"query": "x", "max_spans": 101}):
        with pytest.raises(ValidationError):
            retrieval.RetrievalContextSearchParams(**bad_ctx)
    assert OPS["retrieval.evaluation.compare"].effect is Effect.READ
    assert OPS["retrieval.context.search"].scopes == {"memory:read"}


@pytest.mark.spec("ARBORA-OPS-R063", "ARBORA-OPS-R063.2", "ARBORA-OPS-R063.5")
@pytest.mark.asyncio
async def test_two_algorithms_match_independent_hits_at_k_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch)
    params = _params()
    client = EngineDouble(DecisionEval=_engine_scores)
    answer = await retrieval.handle_retrieval_compare(
        context(client, monkeypatch), params, OPS["retrieval.evaluation.compare"]
    )
    ledger = _ledger(params)
    assert (
        {
            score["algorithm_id"]: score["numerator"]
            for score in answer["value"]["scores"]
        }
        == ledger
        == {"bm25": 1, "dense": 2}
    )
    assert all(score["denominator"] == 2 for score in answer["value"]["scores"])
    sent = client.sent[0][1]["op"]
    assert sent["op"] == "retrieval_ranking_submit"
    assert sent["request"]["tenant_id"] == "tenant-a"
    assert len(sent["request"]["idempotency_key"]) == 64


CONTEXT_TEXT = "café 世界 ok"
BYTES = len(CONTEXT_TEXT.encode())


def _span(**over: Any) -> dict[str, Any]:
    base = {
        "context_start": 10,
        "context_end": 10 + BYTES,
        "context_text": CONTEXT_TEXT,
        "document_id": "doc",
        "document_revision": 1,
        "match_start": 10,
        "match_end": 15,
        "text_digest": "td",
    }
    return {**base, **over}


@pytest.mark.spec("ARBORA-OPS-R063.3")
@pytest.mark.asyncio
async def test_context_spans_slice_original_bytes_with_unicode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "RetrievalContextSearch")
    client = EngineDouble(
        RetrievalContextSearch={
            "spans": [_span()],
            "next_cursor": "c2",
            "index_digest": "i",
            "query_revision": 1,
            "authorization_revision": 3,
        }
    )
    answer = await retrieval.handle_retrieval_context_search(
        context(client, monkeypatch),
        {"query": "caf", "ngram_n": 3, "max_spans": 100},
        OPS["retrieval.context.search"],
    )
    [span] = answer["value"]["spans"]
    assert span["context_text"] == CONTEXT_TEXT
    assert span["context_end"] - span["context_start"] == len(
        span["context_text"].encode()
    )
    request = client.sent[0][1]["request"]
    assert request["max_spans"] == 100 and request["tenant_id"] == "tenant-a"
    assert answer["value"]["next_cursor"] == "c2"


@pytest.mark.spec("ARBORA-OPS-R063.4")
@pytest.mark.asyncio
async def test_every_refusal_returns_before_any_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, "RetrievalContextSearch")
    op = OPS["retrieval.evaluation.compare"]
    cases = {
        "INVALID_ARGUMENT": [
            _params(label_pairs=[{"query_id": "q1", "document_id": "d1"}]),
            _params(
                algorithms=[
                    _run("a", {"q1": ["d1"]}),
                    _run("b", {"q1": ["d1"], "q2": ["d2"]}),
                ]
            ),
            _params(
                algorithms=[
                    _run("a", {"q1": ["d1"], "q2": ["d2"]}),
                    _run("a", {"q1": ["d1"], "q2": ["d2"]}),
                ]
            ),
        ],
        "PLAN_STALE": [
            _params(
                algorithms=[
                    _run("a", {"q1": ["d1"], "q2": ["d2"]}),
                    _run("b", {"q1": ["d1"], "q2": ["d2"]}, digest="other"),
                ]
            )
        ],
    }
    for code, bad_inputs in cases.items():
        for bad in bad_inputs:
            client = EngineDouble(DecisionEval=_engine_scores)
            with pytest.raises(OperationRefused) as refused:
                await retrieval.handle_retrieval_compare(
                    context(client, monkeypatch), bad, op
                )
            assert refused.value.code == code and client.sent == []
    # A page whose byte map disagrees with its text, or that exceeds the bound, refuses.
    for spans in (
        [_span(context_end=10 + BYTES + 1)],
        [_span(match_start=9)],
        [_span(context_end=10 + 9000, context_text="x" * 9000)],
        [_span()] * 101,
    ):
        client = EngineDouble(RetrievalContextSearch={"spans": spans})
        with pytest.raises(OperationRefused) as refused:
            await retrieval.handle_retrieval_context_search(
                context(client, monkeypatch),
                {"query": "q", "ngram_n": 3, "max_spans": 20},
                OPS["retrieval.context.search"],
            )
        assert refused.value.code == "UNAVAILABLE"
    # An engine that does not serve the op refuses typed before any send.
    monkeypatch.undo()
    cold = EngineDouble(DecisionEval=_engine_scores)
    with pytest.raises(OperationRefused) as unavailable:
        await retrieval.handle_retrieval_compare(
            context(cold, monkeypatch), _params(), op
        )
    assert unavailable.value.code == "UNAVAILABLE" and cold.sent == []
