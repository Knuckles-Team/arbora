"""Serving start binds the decide operations (ARBORA-HOST-R020, R021).

Arbora holds no assembler, planner or report. Each install step is guarded: a
missing input or an engine that does not serve the method logs a reason and
skips that binding, and serving continues.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from arbora.mcp_server import decide_wiring
from arbora.mcp_server.decide_wiring import (
    _commit_context,
    _mutation_context,
    _publish_context,
    install_decide_consumers,
)


def _session() -> Any:
    return SimpleNamespace(
        actor=SimpleNamespace(actor_id="actor-1"),
        tenant="tenant-a",
        graph="graph-a",
        policy_version="v7",
        trace_context="trace-1",
    )


def _run(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


@pytest.mark.spec("ARBORA-HOST-R020")
def test_all_three_bind_and_each_guarded_skip(monkeypatch, caplog):
    session = _session()
    engine = object()
    client = object()
    bound_to: list[Any] = []

    def fake_task_plan(eg_client: Any) -> None:
        bound_to.append(eg_client)

    monkeypatch.setattr("arbora.api.ops.plans.engine_task_plan", fake_task_plan)
    with caplog.at_level(logging.INFO, logger=decide_wiring.logger.name):
        installed = install_decide_consumers(lambda _s: client, session, engine)
    assert set(installed) == {
        "assembler",
        "topology",
        "task_planner",
        "cross_source",
        "workflow_lookup",
    }
    # Each binding that the engine does not serve yet is skipped with its reason.
    assert installed["assembler"] is False
    assert installed["cross_source"] is False
    assert "decide.consumers.assembly" in caplog.text
    assert "knowledge_graph.virtual_graph.federation" in caplog.text
    # Serving continues: the install returned a dict instead of raising.
    assert isinstance(installed, dict)


@pytest.mark.spec("ARBORA-HOST-R020")
def test_a_missing_engine_client_skips_every_binding(caplog):
    with caplog.at_level(logging.INFO, logger=decide_wiring.logger.name):
        none_session = install_decide_consumers(lambda _s: object(), None, object())
        no_client = install_decide_consumers(lambda _s: None, _session(), object())
    assert not any(none_session.values())
    assert not any(no_client.values())
    assert "no engine client" in caplog.text


@pytest.mark.spec("ARBORA-HOST-R020")
def test_a_client_factory_fault_never_stops_serving(caplog):
    def boom(_session: Any) -> Any:
        raise RuntimeError("engine down")

    with caplog.at_level(logging.WARNING, logger=decide_wiring.logger.name):
        installed = install_decide_consumers(boom, _session(), object())
    assert not any(installed.values())
    assert "engine down" in caplog.text


@pytest.mark.spec("ARBORA-HOST-R021")
def test_assembly_commits_and_publishes_with_session_context():
    session = _session()
    commit = _run(_commit_context(session)({}))
    publish = _run(_publish_context(session)({}))
    assert commit["purpose_id"] == "agent_library.assembly_commit"
    assert publish["purpose_id"] == "agent_library.assembly_publish"
    for context in (commit, publish):
        assert context["principal"] == "actor-1"
        assert context["caller_principal"] == "actor-1"
        assert context["tenant_id"] == "tenant-a"
        assert context["policy_revision"] == "v7"
        assert context["trace_id"] == "trace-1"
    # Every context is single-use: a fresh nonce and idempotency key each time.
    again = _mutation_context(session, "agent_library.assembly_commit")
    assert again["attempt_nonce"] != commit["attempt_nonce"]
    assert again["idempotency_key"] != commit["idempotency_key"]


@pytest.mark.spec("ARBORA-HOST-R021")
def test_the_topology_asker_skips_with_its_reason_until_the_engine_serves_it(caplog):
    with pytest.raises(Exception) as refused:
        decide_wiring._published_templates()
    assert refused.value.details["spec_row"] == "MEDULLA-DECISION-ENGINE-R105"
