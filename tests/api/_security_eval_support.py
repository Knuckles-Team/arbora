"""Doubles for the security evaluation tests: the engine ``Decide`` and the harness runner."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from arbora.api.ops import _question
from arbora.api.ops.security_eval import (
    EngineSecurityEvaluation,
    EvaluationCatalog,
    ProbeRun,
    ScorerScore,
)
from tests.api._lane_double import EngineDouble, context

Q32 = 1 << 32
SCHEMA = {"component_id": "schema-1", "definition_digest": "d" * 64}


def decide_answer(selected: list[str], verdicts: dict[str, str]) -> Any:
    """Answer ``Decide``: the selection question lists ``selected``; a verdict names one."""

    def answer(params: dict[str, Any]) -> dict[str, Any]:
        request = params["request"]
        kind = request["question"]["kind"]
        if kind == "probe_selection":
            scores = [
                {"option_id": name, "score": {"scale": "q32", "value": Q32 - i}}
                for i, name in enumerate(selected)
            ]
        else:
            attack = next(
                p["value"]["value"]
                for p in request["params"]
                if p["name"] == "attack_id"
            )
            chosen = verdicts.get(attack, "inconclusive")
            scores = [
                {
                    "option_id": label,
                    "score": {"scale": "q32", "value": Q32 if label == chosen else 0},
                }
                for label in ("resisted", "compromised", "inconclusive")
            ]
        return {"records": [{"outcome": {"outcome": "advisory", "scores": scores}}]}

    return answer


class Runner:
    """The harness: records each run and reports scored probes for the target."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str]] = []

    async def run(
        self, context: Any, entries: EvaluationCatalog, target_id: str
    ) -> tuple[ProbeRun, ...]:
        ids = tuple(entry.entry_id for entry in entries.entries)
        self.calls.append((ids, target_id))
        return tuple(
            ProbeRun(
                probe_id=f"{target_id}:{name}",
                attack_id=name,
                category="injection",
                evidence_ref=f"evidence:{target_id}:{name}",
                scores=(ScorerScore(scorer_id="scorer-a", permille=800),),
            )
            for name in ids
        )


def evaluation_context(
    monkeypatch: pytest.MonkeyPatch,
    *,
    selected: list[str] | None = None,
    verdicts: dict[str, str] | None = None,
    scopes: tuple[str, ...] = ("security:target:t1",),
    runner: Any = None,
    bound: bool = True,
) -> tuple[Any, EngineDouble, Runner]:
    """A served context with the engine double and a recording harness runner."""
    import medulla.generated as generated

    monkeypatch.setattr(_question, "require_question_kind", lambda _kind: None)

    async def send(client: Any, method: str, params: Any, graph: str, **kw: Any) -> Any:
        return await client._send(method, params, graph, **kw)

    monkeypatch.setattr(generated, "send_by_id", send)
    names = selected if selected is not None else ["attack-1", "tx-1"]
    engine = EngineDouble(
        Decide=decide_answer(names, verdicts or {"attack-1": "compromised"})
    )
    harness = runner if runner is not None else Runner()
    services: dict[str, Any] = {"security_feature_schema": SCHEMA}
    if bound:
        services["security_evaluation"] = EngineSecurityEvaluation()
        services["security_probe_runner"] = harness
    ctx = context(engine, monkeypatch, services=services)
    ctx.caller = SimpleNamespace(
        **{**vars(ctx.caller), "effective_scopes": frozenset(scopes)}
    )
    return ctx, engine, harness
