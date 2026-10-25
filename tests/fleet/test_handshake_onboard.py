# ruff: noqa: F811
"""MCP handshake as the onboarding trigger (ARBORA-FLEET-R041.7 to R041.10).

The multiplexer announces each completed child handshake; the
:class:`HandshakeOnboarder` schedules one debounced, off-path pass through the
one ``FleetOnboarding.onboard`` function. EG is faked at the transport, as in
``test_fleet_onboarding``.
"""

from __future__ import annotations

import functools
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from arbora.fleet.multiplexer import MCPMultiplexer
from arbora.fleet.onboarding import (
    FleetEndpoint,
    FleetOnboarding,
    HandshakeOnboarder,
    OnboardingTrigger,
    handshake_catalog_digest,
)
from tests.fleet.test_fleet_onboarding import (
    _HEAD_CATALOG,
    ALPHA,
    GRAPH,
    _head_digest,
    _onboarding,
    _RecordingSink,
    _Registry,
    _served_pack,
    eg,  # noqa: F401
)

DEBOUNCE = 0.25


class _Clock:
    """A fake wait: records delays; the debounce wait blocks on a gate."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.gate = threading.Event()
        self.on_wait: Any = None

    def __call__(self, seconds: float) -> bool:
        self.delays.append(seconds)
        if seconds == DEBOUNCE:
            self.gate.wait(10)
        elif self.on_wait is not None:
            self.on_wait(seconds)
        return False


def _tools(*names: str) -> list[Any]:
    return [SimpleNamespace(name=n, inputSchema={"type": "object"}) for n in names]


class _Session:
    """A child session: a discover session has no ``initialize_result``."""

    def __init__(
        self,
        version: str = "1.0.0",
        prompts: Any = (),
        declaration: str = "",
        tools: Any = None,
    ) -> None:
        self.server_info = SimpleNamespace(version=version)
        self.initialize_result = None  # server/discover, not initialize
        self._prompts = list(prompts)
        self._declaration = declaration
        self._tools = tools

    async def list_tools(self) -> Any:
        return SimpleNamespace(tools=self._tools or _tools("lookup"))

    async def list_prompts(self) -> Any:
        return SimpleNamespace(prompts=self._prompts)

    async def list_resources(self) -> Any:
        listed = (
            [SimpleNamespace(uri="connector://resources", mimeType="application/json")]
            if self._declaration
            else []
        )
        return SimpleNamespace(resources=listed)

    async def read_resource(self, uri: str) -> Any:
        return SimpleNamespace(contents=[SimpleNamespace(text=self._declaration)])


def _handshake(
    mux: Any,
    name: str = "alpha-mcp",
    version: str = "1.0.0",
    tools: Any = None,
    prompts: Any = (),
    declaration: str = "",
) -> None:
    """Announce a completed discover handshake through the multiplexer's hook."""

    import asyncio

    session = _Session(version, prompts, declaration, tools)
    asyncio.run(
        MCPMultiplexer._announce_handshake(
            cast(Any, mux), name, session, tools or _tools("lookup")
        )
    )


def _setup(
    registry: _Registry, capture: Any = _served_pack, **kwargs: Any
) -> tuple[HandshakeOnboarder, SimpleNamespace, _Clock]:
    clock = _Clock()
    mux = SimpleNamespace(_fleet_onboarding=None, _handshake_listener=None)
    onboarder = HandshakeOnboarder(
        onboarding=_onboarding(registry, capture),
        multiplexer=mux,
        endpoints=lambda: (ALPHA,),
        debounce_s=DEBOUNCE,
        wait=clock,
        **kwargs,
    )
    mux._handshake_listener = onboarder.notify
    return onboarder, mux, clock


def _run(onboarder: HandshakeOnboarder, clock: _Clock) -> None:
    clock.gate.set()
    onboarder.drain()


def _record(mux: Any) -> dict[str, Any]:
    return mux._fleet_onboarding["surfaces"]["alpha-mcp"]["onboarding"]


# spec: ARBORA-FLEET-R041.7
def test_completed_handshake_starts_one_pass_and_imports_full_pack(
    eg: _Registry,
) -> None:
    """ARBORA-FLEET-R041.7: the handshake alone onboards the connector."""

    onboarder, mux, clock = _setup(eg)

    _handshake(mux)  # nothing else is called
    _run(onboarder, clock)

    assert onboarder.passes_run == 1
    assert eg.attested == ["alpha-mcp"]
    assert [c for c, _b in _RecordingSink.imported] == ["alpha-mcp"]
    assert _record(mux)["state"] == "onboarded"
    assert _record(mux)["reason"] == "handshake"
    trigger = OnboardingTrigger("alpha-mcp", "handshake")
    assert trigger.reason == "handshake"


# spec: ARBORA-FLEET-R041.8
# spec: ARBORA-FLEET-R041.14
def test_reconnect_recaptures_only_on_content_digest_change(
    eg: _Registry,
) -> None:
    """ARBORA-FLEET-R041.8: unchanged reconnect renews; a change re-captures."""

    captured: list[str] = []

    async def capture(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
        captured.append(endpoint.name)
        return await _served_pack(endpoint, auth=auth)

    onboarder, mux, clock = _setup(eg, capture)
    _handshake(mux)
    _run(onboarder, clock)
    assert captured == ["alpha-mcp"]
    registers = len(eg.registers)

    _handshake(mux)  # unchanged reconnect
    onboarder.drain()
    assert captured == ["alpha-mcp"]
    assert _record(mux)["state"] == "renewed"
    assert len(eg.registers) >= registers  # the lease was renewed, not re-imported
    assert eg.attested == ["alpha-mcp"]

    _handshake(mux, version="9.9.9")  # new serverInfo version, same content
    onboarder.drain()
    assert len(captured) == 1  # the version is never a change signal

    _handshake(mux, tools=_tools("lookup", "extra"))  # a served tool changed
    onboarder.drain()
    assert len(captured) == 2

    _handshake(mux, tools=_tools("lookup", "extra"), declaration='{"sources": []}')
    onboarder.drain()
    assert len(captured) == 3  # connector://resources changed

    prompt = SimpleNamespace(
        model_dump=lambda **_k: {"name": "p", "_meta": {"team.knuckles/prompt": {}}}
    )
    _handshake(
        mux,
        tools=_tools("lookup", "extra"),
        declaration='{"sources": []}',
        prompts=[prompt],
    )
    onboarder.drain()
    assert len(captured) == 4  # a prompt changed
    assert _record(mux)["catalog_digest"] != handshake_catalog_digest(
        _tools("lookup", "extra")
    )


# spec: ARBORA-FLEET-R041.14
def test_list_changed_notification_recaptures_through_the_one_pass() -> None:
    """A list_changed notification re-digests the child and notifies the listener."""

    import asyncio

    seen: list[dict[str, Any]] = []
    mux = SimpleNamespace(
        _handshake_listener=lambda name, **kw: seen.append({"name": name, **kw}),
        _list_changed_tasks=set(),
    )
    mux._announce_handshake = functools.partial(
        MCPMultiplexer._announce_handshake, cast(Any, mux)
    )
    session = _Session(prompts=[])
    handler = MCPMultiplexer._child_message_handler(
        cast(Any, mux), "alpha-mcp", [session]
    )

    from mcp_types import PromptListChangedNotification

    async def drive() -> None:
        await handler(PromptListChangedNotification())
        await handler(RuntimeError("transport fault"))  # not a list change: ignored
        await asyncio.gather(*mux._list_changed_tasks)

    asyncio.run(drive())
    assert [item["reason"] for item in seen] == ["list_changed"]
    assert seen[0]["name"] == "alpha-mcp"


# spec: ARBORA-FLEET-R041.8
def test_unchanged_pack_in_eg_renews_without_reattest(eg: _Registry) -> None:
    """A first handshake whose pack EG already holds renews through onboard."""

    import asyncio

    digest = asyncio.run(_head_digest(ALPHA, _HEAD_CATALOG))
    eg.heads["alpha-mcp"] = (_HEAD_CATALOG, digest)
    onboarder, mux, clock = _setup(eg)

    _handshake(mux)
    _run(onboarder, clock)

    assert eg.attested == []
    assert _record(mux)["state"] == "renewed"


# spec: ARBORA-FLEET-R041.9
def test_failed_capture_serves_tools_reports_gap_and_retries_with_backoff(
    eg: _Registry,
) -> None:
    """ARBORA-FLEET-R041.9: two failures, then success; serving never blocks."""

    calls = 0
    states: list[dict[str, Any]] = []

    async def capture(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise ConnectionError("alpha refused the session")
        return await _served_pack(endpoint, auth=auth)

    onboarder, mux, clock = _setup(eg, capture, retry_base_s=5.0, retry_cap_s=8.0)
    clock.on_wait = lambda _s: states.append(dict(_record(mux)))

    _handshake(mux)  # returns at once: the failing pass runs off this path
    assert calls == 0
    _run(onboarder, clock)

    assert calls == 3
    assert clock.delays == [DEBOUNCE, 5.0, 8.0]  # doubled, then capped
    assert [s["state"] for s in states] == ["failed", "failed"]
    assert states[0]["error"].startswith("ConnectionError")
    assert states[0]["attempts"] == 1
    assert states[1]["attempts"] == 2
    assert _record(mux)["state"] == "onboarded"  # reset on success
    assert _record(mux)["attempts"] == 0
    assert "error" not in _record(mux)


# spec: ARBORA-FLEET-R041.9
def test_retry_is_bounded(eg: _Registry) -> None:
    """ARBORA-FLEET-R041.9: the backoff gives up after max_attempts."""

    calls = 0

    async def capture(endpoint: FleetEndpoint, *, auth: Any = None) -> Any:
        nonlocal calls
        calls += 1
        raise ConnectionError("down")

    onboarder, mux, clock = _setup(eg, capture, max_attempts=3)
    _handshake(mux)
    _run(onboarder, clock)

    assert calls == 3
    assert _record(mux)["state"] == "failed"
    assert _record(mux)["attempts"] == 3


# spec: ARBORA-FLEET-R041.7
def test_reconnect_bursts_are_debounced(eg: _Registry) -> None:
    """ARBORA-FLEET-R041.7: a burst of reconnects schedules one pass."""

    onboarder, mux, clock = _setup(eg)

    for _ in range(5):
        _handshake(mux)
    _run(onboarder, clock)

    assert onboarder.passes_run == 1
    assert eg.attested == ["alpha-mcp"]


# spec: ARBORA-FLEET-R041.9
def test_listener_failure_never_breaks_the_handshake() -> None:
    """ARBORA-FLEET-R041.9: a broken listener cannot fail serving."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("scheduler down")

    _handshake(SimpleNamespace(_handshake_listener=boom))
    _handshake(SimpleNamespace(_handshake_listener=None))


# spec: ARBORA-FLEET-R041.10
def test_large_shrink_on_handshake_waits_for_mass_withdrawal_approval(
    eg: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARBORA-FLEET-R041.10: the handshake pass never bypasses EG's hold."""

    imports = 0

    async def held(self: Any, pack: Any, *, allow_mass_withdrawal: bool = False) -> Any:
        nonlocal imports
        assert allow_mass_withdrawal is False  # no operator approval on a handshake
        imports += 1
        violation = SimpleNamespace(
            code=SimpleNamespace(value="PACK_MASS_WITHDRAWAL"),
            uri="",
            detail="drops 9 of 10 tools",
        )
        return SimpleNamespace(result="rejected", violations=[violation])

    monkeypatch.setattr(_RecordingSink, "import_pack", held)
    onboarder, mux, clock = _setup(eg)

    _handshake(mux, tools=_tools("lookup"))  # the reconnect drops most tools
    _run(onboarder, clock)

    assert imports == 1  # one attempt through the one onboard; no retry storm
    assert clock.delays == [DEBOUNCE]
    assert _record(mux)["state"] == "awaiting_approval"
    assert "PACK_MASS_WITHDRAWAL" in _record(mux)["error"]
    assert isinstance(onboarder.onboarding, FleetOnboarding)


# spec: ARBORA-FLEET-R041.18
def test_handshake_pass_scopes_its_loop_transports(
    eg: _Registry, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARBORA-FLEET-R041.18: a pack-changing handshake on the fresh loop.

    The serving loop holds the pool's tenant client while the worker thread's
    ``asyncio.run`` loop runs the pass. Before the scope existed, the pass
    refused with ``live client belongs to another event loop``. The pass now
    owns temporary transports, closes them on exit and leaves the serving
    client in place.
    """

    import asyncio
    import contextlib

    from arbora.epistemic import EpistemicClient, EpistemicClientPool
    from arbora.mcp_server.served_engine import (
        SessionRoutedClient,
        _session_context,
    )
    from tests.fleet.test_fleet_onboarding import _session

    class _LoopClient(EpistemicClient):
        """A pooled transport client bound to the loop that connected it."""

        placement: Any = None
        cluster_topology: Any = None

        def __init__(self, **kwargs: Any) -> None:
            self.graph = kwargs["graph_name"]
            self.context = kwargs.get("verified_context")
            self.loop = asyncio.get_running_loop()
            self.reader = asyncio.create_task(asyncio.Event().wait())
            self.closed = False
            self.close_count = 0

        async def health(self) -> dict[str, Any]:
            return {"status": "ok"}

        @contextlib.contextmanager
        def use_verified_context(self, context: Any) -> Any:
            previous = self.context
            self.context = context
            try:
                yield self
            finally:
                self.context = previous

        async def _send(
            self,
            method: str,
            params: Any,
            graph: Any,
            *,
            idempotency_key: Any = None,
        ) -> Any:
            return await eg._send(
                method, params, graph, idempotency_key=idempotency_key
            )

        async def close(self) -> None:
            assert asyncio.get_running_loop() is self.loop
            self.close_count += 1
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
            self.closed = True

    clients: list[_LoopClient] = []
    monkeypatch.setattr(
        eg, "_on_AddTriples", lambda _params, _key: {"triples": 1}, raising=False
    )

    async def connect(**kwargs: Any) -> _LoopClient:
        client = _LoopClient(**kwargs)
        clients.append(client)
        return client

    pool = EpistemicClientPool(connect)
    monkeypatch.setattr(
        eg,
        "for_graph",
        lambda graph: SimpleNamespace(async_client=SessionRoutedClient(pool, graph)),
    )
    # One tenant for serving and the pass, as in the served process.
    caller = _session_context(_session())

    async def serving() -> None:
        serving_client = await pool.connected_client(caller, GRAPH)

        onboarder, mux, clock = _setup(eg)
        # announce the completed handshake (alpha-mcp has no seen digest: the
        # pass imports); notify is the synchronous scheduling step
        onboarder.notify(
            ALPHA.name,
            server_version="1.0.0",
            digest=handshake_catalog_digest(_tools("lookup")),
        )
        clock.gate.set()
        onboarder.drain()  # join the worker's asyncio.run loop

        assert _record(mux)["state"] == "onboarded"
        assert eg.attested == ["alpha-mcp"]
        assert [name for name, _b in _RecordingSink.imported] == ["alpha-mcp"]
        for client in clients:
            if client is serving_client:
                continue
            assert client.closed and client.close_count == 1
            assert client.loop is not asyncio.get_running_loop()
        assert pool.graph_count == 1
        assert await pool.connected_client(caller, GRAPH) is serving_client
        await pool.close()

    asyncio.run(serving())
