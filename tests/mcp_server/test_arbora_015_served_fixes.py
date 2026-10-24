"""Served-path regressions found by the arbora 0.1.4 live acceptance run."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

# ── fleet.call: probe-primed schema and annotations ──────────────────────────


class _Mux:
    """A multiplexer whose probe fills the cache once per server."""

    def __init__(self, fail: bool = False) -> None:
        self._probe_cache: dict[str, dict[str, Any]] = {}
        self.probed: list[str] = []
        self.fail = fail

    async def probe_server(self, server: str) -> dict[str, Any]:
        self.probed.append(server)
        if self.fail:
            raise RuntimeError("child down")
        info = {
            "tools": [
                {
                    "name": "web_search",
                    "description": "search",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                    "annotations": {"readOnlyHint": True},
                }
            ],
            "error": None,
        }
        self._probe_cache[server] = info
        return info


def test_primer_probes_a_named_server_once_and_never_sweeps() -> None:
    """0.1.4: the served catalog had no schema or annotations for any tool.

    The EG record carries neither; only a probe does, and the served path
    never probed. A read-only tool then resolved as a write, and the caller
    could not learn its parameters (so it called ``web_search`` with ``{}``).
    """
    from arbora.fleet.catalog_composition import probe_primer

    mux = _Mux()
    prime = probe_primer(mux)
    asyncio.run(prime(["searxng-mcp"]))
    asyncio.run(prime(["searxng-mcp"]))
    assert mux.probed == ["searxng-mcp"]


def test_primer_does_not_retry_a_failed_probe_inside_its_window() -> None:
    from arbora.fleet.catalog_composition import probe_primer

    now = [0.0]
    mux = _Mux(fail=True)
    prime = probe_primer(mux, retry_s=60.0, clock=lambda: now[0])
    asyncio.run(prime(["github-mcp"]))
    asyncio.run(prime(["github-mcp"]))
    assert mux.probed == ["github-mcp"]
    now[0] = 61.0
    asyncio.run(prime(["github-mcp"]))
    assert mux.probed == ["github-mcp", "github-mcp"]


@pytest.mark.spec("ARBORA-OPS-R020.6.6")
def test_primer_reprobes_a_cached_probe_that_a_schema_refresh_dropped() -> None:
    """A child remount drops its cached probe; the next request re-primes it.

    The retry window throttles failed probes only. Before, a source sync that
    remounted its child lost the cached probe, the window blocked the
    re-probe, and the read-only list tool resolved as a write (``tool_failed``).
    """
    from arbora.fleet.catalog_composition import probe_primer

    now = [0.0]
    mux = _Mux()
    prime = probe_primer(mux, retry_s=60.0, clock=lambda: now[0])
    asyncio.run(prime(["boot-ingest"]))
    mux._probe_cache.pop("boot-ingest")
    now[0] = 1.0
    asyncio.run(prime(["boot-ingest"]))
    assert mux.probed == ["boot-ingest", "boot-ingest"]
    assert "boot-ingest" in mux._probe_cache


def test_primed_probe_gives_the_catalog_item_its_schema_and_read_effect() -> None:
    from arbora.api.registry import Effect
    from arbora.fleet.catalog_items import CatalogItem, items_from_child_probe
    from arbora.fleet.catalog_sources import _merge_probed_item, _ServerPolicy
    from arbora.fleet.gateway_ops import annotation_effect

    mux = _Mux()
    info = asyncio.run(mux.probe_server("searxng-mcp"))
    verified = CatalogItem(
        id="fleet:tool:searxng-mcp/web_search",
        kind="tool",
        name="web_search",
        server="searxng-mcp",
    )
    merged = {verified.id: verified}
    policy = _ServerPolicy(frozenset({"mcp:delegate"}), frozenset(), "delegated", {})
    for probed in items_from_child_probe("searxng-mcp", info):
        _merge_probed_item(merged, probed, policy=policy, subject_id=None)
    item = merged[verified.id]
    assert item.public()["input_schema"]["required"] == ["query"]
    effect, _confirm, _principal = annotation_effect(item.annotations)
    assert effect == Effect.READ


def test_admitted_tool_primes_its_server_before_reading_the_catalog() -> None:
    from arbora.fleet.multiplexer_ops import MultiplexerOps

    order: list[str] = []

    async def prime(servers: Any) -> None:
        order.append("prime:" + ",".join(servers))

    class _Catalog:
        async def _raw_get(self, item_id: str) -> Any:
            order.append("read")
            return None

    ops = MultiplexerOps(
        catalog=_Catalog(),  # type: ignore[arg-type]
        sessions=SimpleNamespace(),  # type: ignore[arg-type]
        loadable=None,  # type: ignore[arg-type]
        mount=None,  # type: ignore[arg-type]
        notify=None,  # type: ignore[arg-type]
        invoke=None,  # type: ignore[arg-type]
        health=dict,
        prime=prime,
    )
    caller = SimpleNamespace(effective_scopes=frozenset({"mcp:delegate"}))
    with pytest.raises(PermissionError):
        asyncio.run(ops.admitted_tool(caller, "searxng-mcp", "web_search"))
    assert order == ["prime:searxng-mcp", "read"]


# ── fleet.call without an idempotency key ────────────────────────────────────


def _caller(request_id: str) -> Any:
    from arbora.api.invoke import VerifiedCaller

    return VerifiedCaller(
        principal="svc-arbora",
        tenant="t1",
        effective_scopes=frozenset({"mcp:delegate"}),
        engine_claims={},
        principal_kind="service",
        authenticated=True,
        delegated=False,
        credential_kind="bearer",
        policy_revision="default",
        request_id=request_id,
    )


def _op(effect: Any) -> Any:
    from arbora.api.registry import Idempotency

    return SimpleNamespace(
        id="fleet.call", effect=effect, idempotency=Idempotency.KEY_REQUIRED
    )


def test_a_keyless_write_fleet_call_derives_a_deterministic_key() -> None:
    """0.1.4: fleet.call without idempotency_key was INVALID_ARGUMENT for MCP clients."""
    from arbora.api.invoke.pipeline import _derived_fleet_key
    from arbora.api.registry import Effect

    args = {"server": "gitlab-mcp", "tool": "gitlab_projects", "arguments": {"a": 1}}
    first = _derived_fleet_key(_op(Effect.WRITE), args, _caller("mcp:s1:7"))
    again = _derived_fleet_key(_op(Effect.WRITE), args, _caller("mcp:s1:7"))
    other = _derived_fleet_key(_op(Effect.WRITE), args, _caller("mcp:s1:8"))
    assert first is not None and first.startswith("derived:")
    assert first == again
    assert other != first


def test_no_key_is_derived_for_a_read_or_without_a_request_id() -> None:
    from arbora.api.invoke.pipeline import _derived_fleet_key
    from arbora.api.registry import Effect

    args = {"server": "s", "tool": "t", "arguments": {}}
    assert _derived_fleet_key(_op(Effect.READ), args, _caller("mcp:s1:7")) is None
    assert _derived_fleet_key(_op(Effect.WRITE), args, _caller("")) is None
    assert _derived_fleet_key(_op(Effect.WRITE), args, _caller("unassigned")) is None


def test_an_in_process_request_names_itself() -> None:
    from arbora.api.mcp.caller import mcp_request_ref, use_request_ref

    assert mcp_request_ref() == ""
    with use_request_ref("run:r1:abc"):
        assert mcp_request_ref() == "run:r1:abc"
    assert mcp_request_ref() == ""


# ── agent runs: native verbs, RunEvents, the reply ───────────────────────────


def test_the_fleet_agent_toolset_binds_the_served_intent_verbs(monkeypatch) -> None:
    """0.1.4: 'requested native Arbora tool is unavailable' for find/act."""
    from arbora.host_bindings import FLEET_TOOLS
    from arbora.mcp_server import runtime

    calls: list[tuple[str, str | None, str]] = []
    events: list[dict[str, Any]] = []

    def make(name: str) -> Any:
        async def verb(
            op: str | None = None,
            intent: str | None = None,
            params: dict[str, Any] | None = None,
            plan_ref: str | None = None,
            execute: bool = True,
            idempotency_key: str | None = None,
        ) -> dict[str, Any]:
            from arbora.api.mcp.caller import mcp_request_ref

            calls.append((name, op, mcp_request_ref()))
            return {"ok": True, "result": {"value": "x"}}

        verb.__doc__ = f"{name} verb"
        return verb

    for name in FLEET_TOOLS:
        monkeypatch.setitem(runtime.NATIVE_INTENT_TOOLS, name, make(name))

    # A legacy action tool of the same name must not shadow the served verb.
    async def legacy_act(action: str = "") -> str:
        raise AssertionError("legacy tool called")

    monkeypatch.setitem(runtime.REGISTERED_TOOLS, "act", legacy_act)
    from agent_utilities import host_ports

    monkeypatch.setattr(host_ports, "emit_run_event", events.append)
    toolset = runtime.build_native_arbora_toolset(
        list(FLEET_TOOLS), toolset_id="run-a2a-1"
    )
    tools = {tool.name: tool for tool in toolset.tools.values()}
    assert set(tools) == set(FLEET_TOOLS)
    result = asyncio.run(
        tools["act"].function(
            op="fleet.call",
            params={"server": "searxng-mcp", "tool": "web_search", "arguments": {}},
        )
    )
    assert result["ok"] is True
    assert calls[0][:2] == ("act", "fleet.call")
    assert calls[0][2].startswith("run:a2a-1:")
    assert events == [
        {
            "type": "tool_call",
            "run_id": "a2a-1",
            "tool_name": "act",
            "op": "fleet.call",
            "server": "searxng-mcp",
            "tool": "web_search",
            "ok": True,
            "error": None,
        }
    ]


def test_run_events_are_written_through_the_served_engine_add_node(monkeypatch) -> None:
    """0.1.4: the served engine has no typed-batch seam, so no RunEvent was written."""
    from arbora import agent_runs

    written: list[tuple[str, str, dict[str, Any]]] = []

    class _Engine:
        def add_node(self, node_id: str, label: str, properties: Any) -> str:
            written.append((node_id, label, dict(properties)))
            return node_id

    monkeypatch.setattr(agent_runs, "_engine", _Engine)
    agent_runs.record_run_event({"type": "run_started", "run_id": "a2a-1", "step": 0})
    assert len(written) == 1
    node_id, label, properties = written[0]
    assert label == "RunEvent"
    assert node_id.startswith("runevent:a2a-1:")
    assert properties["run_id"] == properties["task_id"] == "a2a-1"
    assert properties["event_type"] == "run_started"


def test_a_failed_run_event_write_never_fails_the_run(monkeypatch) -> None:
    from arbora import agent_runs

    class _Engine:
        def add_node(self, *args: Any) -> str:
            raise RuntimeError("engine down")

    monkeypatch.setattr(agent_runs, "_engine", _Engine)
    agent_runs.record_run_event({"type": "error", "run_id": "a2a-1"})


@pytest.mark.parametrize("agent_name", ["arbora", "webui-assistant", "unrelated-agent"])
def test_the_a2a_run_uses_the_task_id_and_the_fleet_tools(
    monkeypatch, agent_name
) -> None:
    """The run's RunEvents name the task; the fleet agent gets find/act."""
    from arbora.a2a import authority as authority_mod
    from arbora.host_bindings import FLEET_TOOLS

    seen: list[Any] = []

    class _Executor:
        async def execute_agent(self, request: Any, *, session: Any) -> Any:
            seen.append(request)
            return SimpleNamespace(
                run_id=request.run_id, output="see https://x.example"
            )

    auth = authority_mod.WorkItemA2AAuthority.__new__(
        authority_mod.WorkItemA2AAuthority
    )
    auth._executor = _Executor()
    task_id = "a2a-" + "a" * 64

    import arbora.work_items as work_items

    monkeypatch.setattr(work_items, "claim_specific", lambda *a, **k: {"t": 1})
    monkeypatch.setattr(work_items, "commit_result", lambda *a, **k: "committed")
    asyncio.run(
        auth._run(
            object(),
            item_id="workitem:orchestrator:" + task_id,
            session=object(),
            agent_name=agent_name,
            task="search",
            task_id=task_id,
        )
    )
    assert seen[0].run_id == task_id
    assert seen[0].allowed_tools == (
        FLEET_TOOLS if agent_name != "unrelated-agent" else None
    )
    artifacts = authority_mod.task_output_artifacts(task_id)
    assert artifacts is not None
    assert artifacts[0].parts[0].text == "see https://x.example"
    dumped = artifacts[0].model_dump(mode="json", by_alias=True)
    assert dumped["artifactId"] == f"{task_id}:reply"
