"""Skill tier promotion and persona reads (ARBORA-OPS-R957)."""

from __future__ import annotations

from typing import Any

import pytest

from arbora.api.errors import EngineRefusal
from arbora.api.ops import skills_eval, skills_tier
from tests.api._knowledge_support import NO_OP, caller_context


class _Pins:
    def pin_for(self, tenant: str) -> dict[str, Any]:
        return {"component": "pin", "tenant": tenant}


def _record(
    monkeypatch: pytest.MonkeyPatch, answer: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def engine_send(
        context: Any, method: str, params: dict[str, Any], *, idempotent: bool = False
    ) -> Any:
        sent.append((method, params))
        if isinstance(answer, Exception):
            raise answer
        return answer if answer is not None else {"ok": True}

    async def graph(context: Any) -> str:
        return "graph:one"

    for module in (skills_eval, skills_tier):
        monkeypatch.setattr(module, "engine_send", engine_send)
    monkeypatch.setattr(skills_tier, "caller_dispatch_graph", graph)
    return sent


def _context() -> Any:
    context = caller_context()
    context.services = {"task_plan_schema_pins": _Pins()}
    return context


@pytest.mark.spec("ARBORA-OPS-R957")
async def test_promote_by_tier_grant_and_persona_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = _record(monkeypatch)
    context = _context()
    # The reviewer is the verified caller; the engine decides the tier grant.
    await skills_tier.handle_tier_promote(
        context,
        {
            "request": {
                "tier_review": {"tier": "domain"},
                "tier_reviewer": "someone:else",
                "approver_principal": "someone:else",
            }
        },
        NO_OP,
    )
    request = sent[0][1]["request"]
    assert "tier_reviewer" not in request and "approver_principal" not in request
    assert request["tier_review"] == {"tier": "domain"}
    assert request["tenant_id"] == "tenant:one"
    for code in (
        "TIER_SELF_REVIEW",
        "TIER_REVIEW_SCOPE_REQUIRED",
        "TIER_REVIEW_MISSING",
        "ALWAYS_ON_CAPACITY_REACHED",
    ):
        _record(monkeypatch, EngineRefusal(code))
        with pytest.raises(EngineRefusal) as refused:
            await skills_tier.handle_tier_promote(context, {"request": {}}, NO_OP)
        assert refused.value.code == code
    # A persona read is a load-set question with the persona, in the caller's graph.
    sent = _record(monkeypatch, {"records": [{"outcome": {"skills": ["s"]}}]})
    got = await skills_tier.handle_persona_get(
        context, {"persona": "support", "job_text": "persona", "max_tools": None}, NO_OP
    )
    assert got["value"]["records"][0]["outcome"] == {"skills": ["s"]}
    method, body = sent[0]
    decide = body["request"]
    assert method == "Decide" and decide["question"]["kind"] == "load_set"
    value = decide["params"][0]["value"]["value"]
    assert value["persona"] == "support" and value["graph"] == "graph:one"
    _record(monkeypatch, EngineRefusal("PERSONA_UNKNOWN"))
    with pytest.raises(EngineRefusal) as unknown:
        await skills_tier.handle_persona_get(
            context, {"persona": "nobody", "job_text": "persona"}, NO_OP
        )
    assert unknown.value.code == "PERSONA_UNKNOWN"
    # The list pages the Persona nodes.
    sent = _record(monkeypatch, [["p:a", {"name": "a"}], ["p:b", {}], ["p:c", {}]])
    page = await skills_tier.handle_persona_list(context, {"limit": 2}, NO_OP)
    assert sent[0][1] == {"label": "Persona", "limit": 3, "after": None}
    assert [p["id"] for p in page["value"]["personas"]] == ["p:a", "p:b"]
    assert page["value"]["next_cursor"] == "p:b"


@pytest.mark.spec("ARBORA-OPS-R957")
def test_tier_and_persona_operations_are_registered_with_the_review_scope() -> None:
    by_id = {op.id: op for op in skills_tier.operations()}
    assert by_id["skills.tier.promote"].scopes == frozenset({"skills:review"})
    assert by_id["skills.persona.get"].scopes == frozenset({"kg:read"})
    assert set(by_id) == {
        "skills.tier.promote",
        "skills.persona.get",
        "skills.persona.list",
    }
