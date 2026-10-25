"""Actual governed ACP path; model and EG/effect owners are controlled test ports."""

import asyncio
from dataclasses import replace
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock

import acp
import httpx
import pytest
from agent_utilities import host_ports
from pydantic_ai.messages import ModelResponse, TextPart, ThinkingPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.toolsets import FunctionToolset

from arbora import agent_runs, work_items
from arbora.a2a.authority import WorkItemA2AAuthority
from arbora.a2a.routing import OrchestratorA2ARouter
from arbora.a2a.service import A2AService
from arbora.api.http.auth import (
    AmbientHTTPAuthenticator,
    verified_bearer_session_for_request,
)
from arbora.api.invoke.executor import BoundOperationRuntime
from arbora.api.mcp.caller import caller_from_session
from arbora.api.ops.agents import operations
from arbora.api.registry import Registry
from arbora.mcp_server import task_plan_runtime
from tests.a2a.test_operation_bridge import SessionRuntime
from tests.acp.test_http_transport import connected
from tests.acp.test_http_transport import http_server as http_server
from tests.mcp_server.test_task_plan_runtime import bridge as bridge
from tests.mcp_server.test_task_plan_runtime import chain as chain

pytestmark = pytest.mark.spec("ARBORA-ACP-R001.9.3")


@pytest.fixture
async def central(http_server, chain, monkeypatch):
    http, root, _, app = http_server
    from arbora.acp import central_runtime

    original_invoke = central_runtime.invoke
    run_callers = []

    async def traced_invoke(op, *args, **kwargs):
        if op == "agents.run":
            run_callers.append(args[1])
        result = await original_invoke(op, *args, **kwargs)
        if getattr(result, "code", "OK") != "OK":
            import logging

            logging.getLogger(__name__).warning(
                "central test operation %s refused: %s", op, result.code
            )
        return result

    monkeypatch.setattr(central_runtime, "invoke", traced_invoke)
    host, publication, _ = chain
    publication["run_callers"] = run_callers
    publication["token"] = app.state.test_token
    rows, commits, tool_calls = {}, [], []
    started = asyncio.Event()
    block = [False]
    engine = SimpleNamespace(
        work_item_graph="owner-graph",
        graph_compute=SimpleNamespace(
            for_graph=lambda graph: SimpleNamespace(
                async_client=SimpleNamespace(
                    work_items=SimpleNamespace(get_run=AsyncMock(return_value=None))
                )
            )
        ),
    )
    authority = WorkItemA2AAuthority(
        lambda: engine, task_plan_config=host.task_plan_config
    )
    service = A2AService(authority, OrchestratorA2ARouter(lambda: engine))
    runtime = SessionRuntime()
    runtime.bindings = {"a2a": service}
    runtime.dispatch = MethodType(BoundOperationRuntime.dispatch, runtime)
    registry = Registry(
        tuple(
            op
            for op in operations()
            if op.id in {"agents.run", "agents.tasks.get", "agents.tasks.cancel"}
        )
        + tuple(op for op in host.services.registry if op.id != "agents.run")
    )
    host = replace(
        host,
        services=replace(
            host.services,
            registry=registry,
            runtime=runtime,
            external_effect_operations=frozenset({"agents.tasks.cancel"}),
        ),
        authenticator=AmbientHTTPAuthenticator(
            session_for_request=verified_bearer_session_for_request
        ),
    )
    authority.hosted_invocation = host
    monkeypatch.setattr(task_plan_runtime, "caller_from_session", caller_from_session)

    def submit(engine, **kwargs):
        item_id = "work-item:" + kwargs["run_id"].removeprefix("a2a-")
        rows[item_id] = {
            "work_item_id": item_id,
            "kind": "orchestrator_task",
            "status": "ready",
            "metadata": kwargs["metadata"],
        }
        return item_id, True

    def claim(engine, item_id, **kwargs):
        rows[item_id]["status"] = "leased"
        return {
            "claimed": True,
            "tenant": "tenant",
            "lease_holder_ref": "test-worker",
            "lease_epoch": 1,
            "fencing_token": 1,
        }

    def commit(engine, item_id, claim, **kwargs):
        commits.append(kwargs)
        if rows[item_id]["status"] != "cancelled":
            rows[item_id]["status"] = (
                "succeeded" if kwargs["outcome"] == "succeeded" else "failed"
            )

    def cancel(engine, item_id, **kwargs):
        rows[item_id]["status"] = "cancelled"
        return True

    monkeypatch.setattr(work_items, "submit_work_item_atomic", submit)
    monkeypatch.setattr(work_items, "get_work_item", lambda engine, key: rows.get(key))
    monkeypatch.setattr(work_items, "claim_specific", claim)
    monkeypatch.setattr(work_items, "commit_result", commit)
    monkeypatch.setattr(work_items, "cancel_work_item", cancel)
    _, eg = task_plan_runtime._contracts()

    async def publish(client, params, graph):
        assert graph == "owner-graph"
        publication["published"].append(params["request"])
        publication["snapshot"] = params["request"]["snapshot"]
        return SimpleNamespace(
            run_id=publication["snapshot"]["run_id"],
            revision=publication["snapshot"]["revision"],
        )

    eg.send_publish_current_plan = publish
    tools = FunctionToolset()

    @tools.tool_plain
    def find() -> str:
        tool_calls.append("find")
        return "private tool detail"

    received_tools = []

    def bind_tools(names, toolset_id):
        received_tools.append(names)
        return tools

    host_ports.register_native_toolset_provider(
        host_ports.NativeToolsetProvider(
            available_tools=lambda: ["find", "act"], build_toolset=bind_tools
        )
    )

    async def model(messages, info):
        if not info.function_tools:
            return ModelResponse(
                parts=[
                    TextPart(
                        '{"tasks":[{"task_id":"answer","prompt":"Answer","depends_on":[]}],"output_task_id":"answer"}'
                    )
                ]
            )
        started.set()
        if block[0]:
            await asyncio.Event().wait()
        if any(
            part.part_kind == "tool-return"
            for message in messages
            for part in message.parts
        ):
            return ModelResponse(parts=[TextPart("central answer")])
        return ModelResponse(
            parts=[ThinkingPart("private thought"), ToolCallPart("find", {})]
        )

    controlled = FunctionModel(model)
    monkeypatch.setattr(agent_runs, "resolve_model", lambda name: controlled)
    host_ports.register_model_provider(lambda name: controlled)
    http.headers["authorization"] = "Bearer " + app.state.test_token(
        scope="kg:read kg:write query:decide"
    )
    app.state.acp_agent_factory = None
    host.configure_acp(app, trusted_roots=frozenset({str(root)}))
    publication["factory"] = app.state.acp_agent_factory
    try:
        yield (
            http,
            root,
            rows,
            commits,
            tool_calls,
            received_tools,
            started,
            block,
            publication,
        )
    finally:
        await app.state.acp_agent_factory.aclose()
        host_ports.register_native_toolset_provider(None)


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_central_actual_dependency_run_projects_only_committed_answer(central):
    http, root, rows, commits, tool_calls, bindings, _, _, publication = central
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        result = await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("help")]
        )
        assert result.stop_reason == "end_turn"
        assert (
            "".join(update.content.text for update in client.updates)
            == "central answer"
        )
        assert tool_calls == ["find"] and "find" in bindings[0]
        assert len(publication["published"]) == 1
        assert commits[-1]["outcome"] == "succeeded"
        assert next(iter(rows.values()))["metadata"]["host_transport_protocol"] == "acp"
        with pytest.raises(acp.RequestError):
            await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("continue")]
            )
        assert len(rows) == 1
        await conn.close_session(session_id=session.session_id)


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_central_cancel_waits_for_owner_cancel_and_readback(central):
    http, root, rows, commits, _, _, started, block, _ = central
    block[0] = True
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        pending = asyncio.create_task(
            conn.prompt(session_id=session.session_id, prompt=[acp.text_block("wait")])
        )
        await asyncio.wait_for(started.wait(), 5)
        assert not client.updates  # Intermediate dependency-node text is not public.
        await conn.cancel(session_id=session.session_id)
        assert (await asyncio.wait_for(pending, 5)).stop_reason == "cancelled"
        assert next(iter(rows.values()))["status"] == "cancelled"
        assert not commits
        await conn.close_session(session_id=session.session_id)


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_same_identity_renewal_feeds_current_prompt_not_creation_credentials(
    central,
):
    import time

    http, root, rows, _, _, _, _, _, publication = central
    http.timeout = httpx.Timeout(20)
    old_expiry = int(time.time()) + 5
    scopes = "kg:read kg:write query:decide"
    http.headers["authorization"] = "Bearer " + publication["token"](
        scope=scopes, exp=old_expiry
    )
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        new_expiry = old_expiry + 120
        http.headers["authorization"] = "Bearer " + publication["token"](
            scope=scopes, exp=new_expiry
        )
        await conn.new_session(cwd=str(root))  # Renew on a real authenticated message.
        await asyncio.sleep(max(0, old_expiry - time.time()) + 0.1)
        result = await conn.prompt(
            session_id=session.session_id, prompt=[acp.text_block("renewed")]
        )
        assert result.stop_reason == "end_turn"
        assert len(rows) == 1
        caller = publication["run_callers"][0]
        assert caller.session.actor.credential_expires_at == new_expiry
        assert caller.principal == "alice" and caller.tenant == "tenant"
        assert (
            "".join(update.content.text for update in client.updates)
            == "central answer"
        )
        await conn.close_session(session_id=session.session_id)


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_central_unclaimed_task_is_not_reported_complete(central, monkeypatch):
    http, root, rows, commits, calls, _, _, _, publication = central
    monkeypatch.setattr(work_items, "claim_specific", lambda *args, **kwargs: None)
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        with pytest.raises(acp.RequestError) as caught:
            await conn.prompt(
                session_id=session.session_id, prompt=[acp.text_block("help")]
            )
        assert caught.value.data["state"] == "submitted"
        assert not client.updates and not calls and not commits
        assert not publication["published"]
        assert next(iter(rows.values()))["status"] == "ready"


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_central_cancel_refusal_is_unknown_not_cancelled(central, monkeypatch):
    http, root, rows, _, _, _, started, block, _ = central
    block[0] = True
    monkeypatch.setattr(work_items, "cancel_work_item", lambda *args, **kwargs: False)
    async with connected(http, root) as (conn, client, path):
        session = await conn.new_session(cwd=str(root))
        pending = asyncio.create_task(
            conn.prompt(session_id=session.session_id, prompt=[acp.text_block("wait")])
        )
        await asyncio.wait_for(started.wait(), 5)
        await conn.cancel(session_id=session.session_id)
        with pytest.raises(acp.RequestError) as caught:
            await asyncio.wait_for(pending, 5)
        assert caught.value.data["reason"] == "cancellation_unconfirmed"
        assert not client.updates
        assert next(iter(rows.values()))["status"] != "cancelled"


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_central_cancel_during_admission_uses_returned_owner_identity(
    central, monkeypatch
):
    import threading

    http, root, rows, commits, calls, _, _, _, publication = central
    entered, release = threading.Event(), threading.Event()
    original = work_items.submit_work_item_atomic

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(work_items, "submit_work_item_atomic", delayed)
    try:
        async with connected(http, root) as (conn, client, path):
            session = await conn.new_session(cwd=str(root))
            pending = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("wait")]
                )
            )
            assert await asyncio.to_thread(entered.wait, 2)
            await conn.cancel(session_id=session.session_id)
            await asyncio.sleep(0.05)
            release.set()
            assert (await asyncio.wait_for(pending, 5)).stop_reason == "cancelled"
            assert len(rows) == 1 and next(iter(rows.values()))["status"] == "cancelled"
            assert not commits and not calls and not publication["published"]
            assert not client.updates
    finally:
        release.set()


@pytest.mark.parametrize("http_server", ["jwt"], indirect=True)
async def test_shutdown_does_not_sever_late_admission_settlement(central, monkeypatch):
    import threading

    http, root, rows, commits, calls, _, _, _, publication = central
    factory = publication["factory"]
    http.timeout = httpx.Timeout(20)
    entered, release = threading.Event(), threading.Event()
    original = work_items.submit_work_item_atomic

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(*args, **kwargs)

    monkeypatch.setattr(work_items, "submit_work_item_atomic", delayed)
    try:
        async with connected(http, root) as (conn, client, path):
            session = await conn.new_session(cwd=str(root))
            pending = asyncio.create_task(
                conn.prompt(
                    session_id=session.session_id, prompt=[acp.text_block("wait")]
                )
            )
            assert await asyncio.to_thread(entered.wait, 2)
            await conn.cancel(session_id=session.session_id)
            with pytest.raises(acp.RequestError) as caught:
                await asyncio.wait_for(pending, 7)
            assert caught.value.data["reason"] == "cancellation_unconfirmed"
            with pytest.raises(RuntimeError, match="shutdown outcome unknown"):
                await factory.aclose()
            owned = tuple(factory.operations)
            assert len(owned) == 1 and not owned[0].done()
            release.set()
            await asyncio.wait_for(asyncio.gather(*owned, return_exceptions=True), 3)
            assert len(rows) == 1 and next(iter(rows.values()))["status"] == "cancelled"
            assert not commits and not calls and not publication["published"]
            assert not client.updates
    finally:
        release.set()
