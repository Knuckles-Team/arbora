"""The served job find loads only the engine's answered tools (ARBORA-FLEET-R037.6)."""

from __future__ import annotations

import pytest

from tests.fleet.test_job_loads import _find, _Gateway, _served


@pytest.mark.spec("ARBORA-FLEET-R037.6")
@pytest.mark.asyncio
async def test_find_job_lazily_loads_answered_tools(monkeypatch) -> None:
    catalog = [f"c{i}.tool-{n}" for i in range(3) for n in range(100)]
    answered = ["c1.tool-4", "c0.tool-9", "c2.tool-77"]
    gateway = _Gateway(cap=8)
    context, _ = _served(
        monkeypatch, {"answer": {"tools": answered}, "decision_id": "dec-4"}
    )
    context.services = {"fleet_gateway": gateway}
    out = (await _find(context, gateway, job_text="search the web"))["value"]
    assert len(catalog) == 300
    assert out["loaded"] == answered
    assert gateway.loaded == [tuple(answered)]
