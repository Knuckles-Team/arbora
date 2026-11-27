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


# --- R037.3 to R037.6: the served job find -----------------------------------

from types import SimpleNamespace  # noqa: E402

from arbora.api.errors import EngineRefusal  # noqa: E402
from arbora.api.invoke.pipeline import OperationRefused  # noqa: E402
from arbora.api.ops import fleet as fleet_ops  # noqa: E402
from arbora.fleet import job_loads  # noqa: E402


class _Gateway:
    def __init__(self, cap: int = 3) -> None:
        self.sessions = SimpleNamespace(cap=cap)
        self.loaded: list[tuple[str, ...]] = []
        self.searched = 0

    async def load_job_answer(self, caller, answer):
        result = job_loads.apply_load_set(answer, self.sessions.cap)
        self.loaded.append(result.loaded)
        return result

    async def search(self, caller, **params):
        self.searched += 1
        return {"items": []}


def _served(monkeypatch, response, *, error=None):
    sent: list[dict] = []

    async def send(context, method, params, **_):
        sent.append({"method": method, "params": params})
        if error is not None:
            raise error
        return response

    async def graph(context):
        return "g1"

    monkeypatch.setattr("arbora.api.ops.engine_calls.engine_send", send)
    monkeypatch.setattr("arbora.api.invoke.bootstrap.caller_dispatch_graph", graph)
    monkeypatch.setattr(
        "arbora.api.ops.skills_tier._feature_schema", lambda c, t: {"features": []}
    )
    monkeypatch.setattr(
        "medulla.decision_client.load_set_request",
        lambda tenant, qid, question, schema, **_: {
            "tenant": tenant,
            "question": question,
        },
    )
    context = SimpleNamespace(caller=SimpleNamespace(tenant="tenant:one"))
    return context, sent


async def _find(context, gateway, **params):
    op = SimpleNamespace(id="fleet.catalog.search")
    return await fleet_ops.handle_fleet_operation(context, params, op)


@pytest.mark.spec("ARBORA-FLEET-R037.3")
@pytest.mark.asyncio
async def test_find_job_reaches_load_tools(monkeypatch) -> None:
    gateway = _Gateway()
    response = {
        "answer": {"tools": ["web.search", "web.fetch"], "skills": [], "agents": []},
        "decision_id": "dec-9",
    }
    context, sent = _served(monkeypatch, response)
    context.services = {"fleet_gateway": gateway}
    out = await _find(context, gateway, job_text="search the web")
    assert gateway.loaded == [("web.search", "web.fetch")]
    assert out["value"]["loaded"] == ["web.search", "web.fetch"]
    assert sent[0]["method"] == "Decide"
    assert sent[0]["params"]["request"]["tenant"] == "tenant:one"
    assert gateway.searched == 0


@pytest.mark.spec("ARBORA-FLEET-R037.4")
@pytest.mark.asyncio
async def test_job_load_refusals(monkeypatch) -> None:
    gateway = _Gateway()
    context, _ = _served(
        monkeypatch, {"abstain": {"reason": "insufficient_confidence"}}
    )
    context.services = {"fleet_gateway": gateway}
    out = await _find(context, gateway, job_text="job")
    assert out["value"] == {
        "loaded": [],
        "abstain_reason": {"reason": "insufficient_confidence"},
    }
    context, _ = _served(monkeypatch, {}, error=ConnectionError("down"))
    context.services = {"fleet_gateway": gateway}
    out = await _find(context, gateway, job_text="job")
    assert out["value"] == {"loaded": [], "refusal": "decision_unavailable"}
    context, _ = _served(monkeypatch, {}, error=EngineRefusal("PARAMETER_INVALID"))
    context.services = {"fleet_gateway": gateway}
    with pytest.raises(EngineRefusal) as refused:
        await _find(context, gateway, job_text="job")
    assert refused.value.code == "PARAMETER_INVALID"
    assert gateway.loaded == [] and gateway.searched == 0
    with pytest.raises(OperationRefused):
        await fleet_ops.handle_fleet_operation(
            context, {"job_text": "x"}, SimpleNamespace(id="fleet.catalog.list")
        )


@pytest.mark.spec("ARBORA-FLEET-R037.5")
@pytest.mark.asyncio
async def test_result_cites_decision(monkeypatch) -> None:
    gateway = _Gateway()
    response = {
        "answer": {
            "tools": ["a", "b"],
            "premises": ["load_set:component=a", {"id": "load_set:component=b"}],
        },
        "decision_id": "dec-3",
    }
    context, _ = _served(monkeypatch, response)
    context.services = {"fleet_gateway": gateway}
    out = (await _find(context, gateway, job_text="job"))["value"]
    assert out["decision_id"] == "dec-3"
    assert out["premises"] == {
        "a": "load_set:component=a",
        "b": "load_set:component=b",
    }
