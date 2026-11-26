"""Per-job loads apply the engine's answer, in its order (ARBORA-FLEET-R037.1, R037.2)."""

from __future__ import annotations

import pytest

from arbora.fleet.job_loads import JobLoadRequest, apply_load_set


@pytest.mark.spec("ARBORA-FLEET-R037.1")
def test_job_load_round_trips() -> None:
    request = JobLoadRequest("search the web", ["web.search"])
    question = request.question(max_tools=8, max_agents=2)
    assert question == {
        "job_text": "search the web",
        "required_capabilities": ["web.search"],
        "max_tools": 8,
        "max_agents": 2,
    }
    with pytest.raises(ValueError):
        JobLoadRequest("   ")


@pytest.mark.spec("ARBORA-FLEET-R037.2")
def test_loads_only_answered_tools() -> None:
    catalog = [f"tool-{i}" for i in range(300)]
    answered = ["tool-250", "tool-3", "tool-99", "tool-7"]
    answer = {
        "tools": [{"id": t, "premise": f"load_set:component={t}"} for t in answered],
        "skills": ["skill-a"],
        "agents": ["agent-x"],
        "decision_id": "dec-1",
    }
    result = apply_load_set(answer, session_cap=3)
    assert result.loaded == ("tool-250", "tool-3", "tool-99")
    assert result.over_cap == ("tool-7",)
    assert set(result.loaded) <= set(catalog) and "tool-0" not in result.loaded
    assert result.skills == ("skill-a",) and result.agent_starts == ("agent-x",)
    assert result.decision_id == "dec-1"
    assert result.premises == {t: f"load_set:component={t}" for t in result.loaded}
    assert apply_load_set({}, 5).loaded == ()


@pytest.mark.spec("ARBORA-FLEET-R037.2")
async def test_multiplexer_ops_loads_the_answer_under_the_session_cap() -> None:
    from types import SimpleNamespace

    from arbora.fleet.multiplexer_ops import MultiplexerOps

    loaded: list[tuple] = []

    async def load_tools(caller, key, items, **kw):
        loaded.append((key, list(items)))

    host = SimpleNamespace(
        sessions=SimpleNamespace(cap=2),
        _session_key_for=lambda caller: "session-1",
        load_tools=load_tools,
    )
    answer = {"tools": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}], "decision_id": "d"}
    result = await MultiplexerOps.load_job_answer(host, object(), answer)
    assert loaded == [("session-1", ["t1", "t2"])]
    assert result.over_cap == ("t3",)
