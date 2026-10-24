"""Caller-bound async source evidence remains a zero-effect prerequisite."""

import asyncio

import pytest

from arbora.access.action_policy import ActionPolicy
from tests.security.test_anonymous_mode import local_mode as local_mode
from tests.test_served_engine_native_labels import native_engine as native_engine

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.6")


def test_released_native_async_source_matches_sync(native_engine, tmp_path):
    engine, session = native_engine
    path = tmp_path / "operator-policy.json"
    path.write_text('{"rules": [{"kind": "observe", "tier": "auto"}]}')
    policy = ActionPolicy(engine, policy_path=path)
    engine.create_node("async-probe", "AsyncProbe", {"value": 7})
    expected = policy.qualified_source_revision()

    async def run():
        try:
            for _ in range(8):
                assert await policy.qualified_source_revision_async() == expected
                assert await engine.query_cypher_async(
                    "MATCH (n:AsyncProbe {value: $value}) RETURN n.value AS value",
                    {"value": 7},
                    session=session,
                ) == [{"value": 7}]
        finally:
            await engine.pool.close()

    asyncio.run(run())


@pytest.mark.parametrize("count,blanket", [(1, False), (16, False), (1, True)])
def test_cancelled_file_task_retains_worker_and_never_queries(
    native_engine, tmp_path, monkeypatch, count, blanket
):
    import threading

    from arbora.access import action_policy_source

    engine, _session = native_engine
    path = tmp_path / "policy.json"
    path.write_text('{"rules": []}')
    policy = ActionPolicy(engine, policy_path=path)
    started, release = threading.Event(), threading.Event()
    lock, workers = threading.Lock(), []
    original = action_policy_source._validated_document
    queries = []

    def blocked_file(selected):
        with lock:
            workers.append(selected)
            if len(workers) == 1:
                started.set()
        assert release.wait(10)
        return original(selected)

    async def forbidden_query(*args, **kwargs):
        queries.append(args)
        raise AssertionError("cancelled file stage must never query")

    monkeypatch.setattr(action_policy_source, "_validated_document", blocked_file)
    monkeypatch.setattr(type(engine), "query_cypher_async", forbidden_query)

    async def run():
        tasks = [
            asyncio.create_task(policy.qualified_source_revision_async())
            for _ in range(count)
        ]
        try:
            while not started.is_set():
                await asyncio.sleep(0.001)
            targets = (
                asyncio.all_tasks() - {asyncio.current_task()} if blanket else tasks
            )
            for task in targets:
                task.cancel()
            await asyncio.sleep(0)
            for task in targets:
                task.cancel()
            for _ in range(5):
                await asyncio.sleep(0)
            assert all(not task.done() for task in tasks)
        finally:
            release.set()
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert not queries

    asyncio.run(run())


@pytest.mark.parametrize("selection", ["path", "engine", "option"])
def test_source_selection_substitution_during_file_await_refuses(
    native_engine, tmp_path, monkeypatch, selection
):
    from arbora.access import action_policy_source

    engine, _session = native_engine
    path = tmp_path / "policy.json"
    path.write_text('{"rules": []}')
    policy = ActionPolicy(engine, policy_path=path)
    original = action_policy_source._retained_file_document

    async def substitute(selected):
        result = await original(selected)
        if selection == "path":
            policy.policy_path = tmp_path / "other.json"
        elif selection == "engine":
            policy.engine = object()
        else:
            policy._irreversibility_aversion = not policy._irreversibility_aversion
        return result

    monkeypatch.setattr(action_policy_source, "_retained_file_document", substitute)
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(policy.qualified_source_revision_async())


def test_async_source_failure_redacts_surrogate(native_engine, tmp_path):
    import json

    engine, _session = native_engine
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"rules": [], "note": "synthetic-marker\ud800"}))
    policy = ActionPolicy(engine, policy_path=path)
    with pytest.raises(RuntimeError, match="unavailable") as caught:
        asyncio.run(policy.qualified_source_revision_async())
    assert caught.value.__context__ is None
    assert caught.value.__cause__ is None
    assert not hasattr(caught.value, "object")


def test_async_source_rechecks_mode_after_query(native_engine, tmp_path, monkeypatch):
    from arbora.security import anonymous

    engine, _session = native_engine
    path = tmp_path / "policy.json"
    path.write_text('{"rules": []}')
    policy = ActionPolicy(engine, policy_path=path)
    original = type(engine).query_cypher_async

    async def revoke(self, *args, **kwargs):
        rows = await original(self, *args, **kwargs)
        monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "required")
        return rows

    monkeypatch.setattr(type(engine), "query_cypher_async", revoke)

    async def run():
        try:
            with pytest.raises(RuntimeError, match="unavailable"):
                await policy.qualified_source_revision_async()
        finally:
            await engine.pool.close()

    asyncio.run(run())


def test_equal_priority_override_order_is_stable():
    from arbora.access.action_policy_source import _rules_from_rows

    rows = [
        {"r": {"kind": "observe", "tier": "forbidden", "priority": 2}},
        {"r": {"kind": "observe", "tier": "auto", "priority": 2}},
        {"r": {"kind": "observe", "tier": "approval_required", "priority": 3}},
    ]
    assert [rule.tier for rule in _rules_from_rows(rows)] == [
        "approval_required",
        "forbidden",
        "auto",
    ]


def test_async_query_rechecks_mode_after_connection(native_engine, monkeypatch):
    from contextlib import asynccontextmanager

    from arbora.security import anonymous

    engine, session = native_engine
    original = type(engine.pool).bind

    @asynccontextmanager
    async def revoke(self, *args, **kwargs):
        async with original(self, *args, **kwargs) as client:
            monkeypatch.setattr(anonymous, "_ACTIVE_MODE", "required")
            yield client

    monkeypatch.setattr(type(engine.pool), "bind", revoke)

    async def run():
        try:
            with pytest.raises(PermissionError):
                await engine.query_cypher_async(
                    "MATCH (n:AsyncProbe) RETURN n", session=session
                )
        finally:
            await engine.pool.close()

    asyncio.run(run())
