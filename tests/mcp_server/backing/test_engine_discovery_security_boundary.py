"""Security contract for KG MCP discovery and the shared child probe."""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from graph_os.fleet import multiplexer as multiplexer_module
from graph_os.fleet.multiplexer import (
    MCPMultiplexer,
    _bounded_tool_catalog,
)
from tests.mcp_server.backing._support import FakeStdioSession


def _remote_config(*, credential_ref: str = "env://SYNTHETIC_TOKEN") -> dict:
    return {
        "mcpServers": {
            "synthetic-child": {
                "url": "https://mcp.example.invalid/mcp",
                "transport": "streamable-http",
                "headers": {"Authorization": credential_ref},
                "tls_profile_ref": "env://SYNTHETIC_TLS_PROFILE",
            }
        }
    }


@pytest.mark.asyncio
async def test_probe_rejects_inline_or_forged_credentials_before_connect() -> None:
    declaration = {
        "url": "https://mcp.example.invalid/mcp",
        "headers": {"Authorization": "synthetic-secret"},
        "_runtime_materialized_secret_keys": ["Authorization"],
        "_runtime_materialization_attestation": "0" * 64,
    }
    with pytest.raises(RuntimeError, match="runtime references"):
        await MCPMultiplexer.probe_declaration(
            "synthetic-child",
            declaration,
            timeout=1.0,
        )


def test_tool_catalog_enforces_count_size_and_depth_bounds() -> None:
    tool = SimpleNamespace(
        name="inspect",
        description="safe",
        input_schema={"type": "object"},
        annotations=None,
    )
    with pytest.raises(RuntimeError, match="exceeded"):
        _bounded_tool_catalog([tool] * 2_049)

    oversized = SimpleNamespace(
        name="inspect",
        description="x" * (4 * 1024 * 1024),
        input_schema={},
        annotations=None,
    )
    with pytest.raises(RuntimeError, match="exceeded"):
        _bounded_tool_catalog([oversized])

    schema: dict = {}
    cursor = schema
    for _ in range(40):
        child: dict = {}
        cursor["nested"] = child
        cursor = child
    too_deep = SimpleNamespace(
        name="inspect",
        description="safe",
        input_schema=schema,
        annotations=None,
    )
    with pytest.raises(RuntimeError, match="exceeded"):
        _bounded_tool_catalog([too_deep])


def _env_recording_stdio(observed: dict):
    """A ``stdio_client`` stand-in that records the child environment."""

    @contextlib.asynccontextmanager
    async def fake_stdio(parameters, *, errlog):
        observed.update(parameters.env or {})
        assert errlog is not None
        yield "read", "write"

    return fake_stdio


@pytest.mark.asyncio
async def test_stdio_probe_delegates_only_allowlisted_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict = {}

    fake_stdio = _env_recording_stdio(observed)
    monkeypatch.setenv("UNRELATED_PRIVATE_TOKEN", "synthetic-secret")
    monkeypatch.setattr(multiplexer_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(multiplexer_module, "ClientSession", FakeStdioSession)
    multiplexer = MCPMultiplexer(tmp_path / "unused.json")

    async with contextlib.AsyncExitStack() as stack:
        await multiplexer._open_one_session(
            "synthetic-child",
            {
                "command": "synthetic-child",
                "args": [],
                "env": {"EXPLICIT_SETTING": "enabled"},
                "initialization_timeout": 1.0,
            },
            stack,
        )

    assert observed["EXPLICIT_SETTING"] == "enabled"
    assert "UNRELATED_PRIVATE_TOKEN" not in observed


@pytest.mark.asyncio
async def test_stdio_provider_profile_is_preflighted_off_loop_and_projected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    observed: dict[str, str] = {}

    fake_stdio = _env_recording_stdio(observed)
    monkeypatch.setattr(multiplexer_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(multiplexer_module, "ClientSession", FakeStdioSession)
    parent_config_root = tmp_path / "parent-private-config"
    parent_config_root.mkdir()
    (parent_config_root / "config.json").write_text(
        '{"LANGFUSE_SECRET_KEY_REF":"env://UNRELATED_RUNTIME_SECRET"}',
        encoding="utf-8",
    )
    (parent_config_root / "runtime-secrets.json").write_text(
        '{"UNRELATED_RUNTIME_SECRET":"must-not-cross"}',
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_UTILITIES_CONFIG_DIR", str(parent_config_root))
    monkeypatch.setenv("UNRELATED_RUNTIME_SECRET", "must-not-cross")
    case_variant_roots = {
        "agent_utilities_config_dir": parent_config_root,
        "xdg_config_home": parent_config_root / "xdg-private",
        "home": parent_config_root / "home-private",
    }
    for key, value in case_variant_roots.items():
        monkeypatch.setenv(key, str(value))
    resolver_threads: list[int] = []

    def prepare(profile_name: str):
        resolver_threads.append(threading.get_ident())
        return SimpleNamespace(
            environment={
                "AGENT_PROVIDER_PROFILE": profile_name,
                "PROVIDER_CONFIGS": '{"synthetic-provider":{}}',
                "SYNTHETIC_RUNTIME_VALUE": "ephemeral",
            },
            close=lambda: None,
        )

    monkeypatch.setattr(
        "agent_utilities.core.provider_runtime.prepare_provider_runtime_child_environment",
        prepare,
    )
    multiplexer = MCPMultiplexer(tmp_path / "unused.json")

    async with contextlib.AsyncExitStack() as stack:
        await multiplexer._open_one_session(
            "synthetic-child",
            {
                "command": "synthetic-child",
                "provider_profile": "synthetic-provider",
                "initialization_timeout": 1.0,
            },
            stack,
        )
        isolated_config_root = Path(observed["AGENT_UTILITIES_CONFIG_DIR"])
        assert isolated_config_root != parent_config_root
        assert list(isolated_config_root.iterdir()) == []
        assert "UNRELATED_RUNTIME_SECRET" not in observed
        assert not set(case_variant_roots).intersection(observed)

    assert observed["AGENT_PROVIDER_PROFILE"] == "synthetic-provider"
    assert observed["SYNTHETIC_RUNTIME_VALUE"] == "ephemeral"
    assert resolver_threads
    assert all(identifier != threading.get_ident() for identifier in resolver_threads)
    assert not isolated_config_root.exists()


@pytest.mark.asyncio
async def test_attested_child_cannot_override_parent_provider_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "agent_utilities.core.provider_runtime.prepare_provider_runtime_child_environment",
        lambda profile_name: SimpleNamespace(
            environment={
                "AGENT_PROVIDER_PROFILE": profile_name,
                "PROVIDER_CONFIGS": '{"synthetic-provider":{}}',
                "SYNTHETIC_RUNTIME_VALUE": "parent-controlled",
            },
            close=lambda: None,
        ),
    )
    declaration = multiplexer_module.attest_runtime_child_config(
        {
            "command": "synthetic-child",
            "provider_profile": "synthetic-provider",
            "env": {"SYNTHETIC_RUNTIME_VALUE": "changed-provider"},
        }
    )
    multiplexer = MCPMultiplexer(tmp_path / "unused.json")

    async with contextlib.AsyncExitStack() as stack:
        with pytest.raises(RuntimeError, match="parent-controlled"):
            await multiplexer._open_one_session(
                "synthetic-child",
                declaration,
                stack,
            )


@pytest.mark.asyncio
async def test_timed_out_provider_resolution_is_bounded_and_erased(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed = threading.Event()

    def delayed_prepare(profile_name: str):
        time.sleep(0.05)
        return SimpleNamespace(
            environment={"AGENT_PROVIDER_PROFILE": profile_name},
            close=closed.set,
        )

    monkeypatch.setattr(
        "agent_utilities.core.provider_runtime.prepare_provider_runtime_child_environment",
        delayed_prepare,
    )
    multiplexer = MCPMultiplexer(tmp_path / "unused.json")

    async with contextlib.AsyncExitStack() as stack:
        with pytest.raises(RuntimeError, match="profile is unavailable"):
            await multiplexer._open_one_session(
                "synthetic-child",
                {
                    "command": "synthetic-child",
                    "provider_profile": "synthetic-provider",
                    "initialization_timeout": 0.005,
                },
                stack,
            )

    assert await asyncio.to_thread(closed.wait, 1.0)


@pytest.mark.asyncio
async def test_provider_projection_closes_when_child_sandbox_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    closed = False

    def prepare(profile_name: str):
        def close() -> None:
            nonlocal closed
            closed = True

        return SimpleNamespace(
            environment={"AGENT_PROVIDER_PROFILE": profile_name},
            close=close,
        )

    def fail_sandbox(_stack) -> dict[str, str]:
        raise OSError("synthetic sandbox creation failure")

    monkeypatch.setattr(
        "agent_utilities.core.provider_runtime.prepare_provider_runtime_child_environment",
        prepare,
    )
    monkeypatch.setattr(
        multiplexer_module,
        "_provider_child_sandbox_environment",
        fail_sandbox,
    )
    multiplexer = MCPMultiplexer(tmp_path / "unused.json")

    with pytest.raises(OSError, match="sandbox creation failure"):
        async with contextlib.AsyncExitStack() as stack:
            await multiplexer._open_one_session(
                "synthetic-child",
                {
                    "command": "synthetic-child",
                    "provider_profile": "synthetic-provider",
                    "initialization_timeout": 1.0,
                },
                stack,
            )

    assert closed is True


@pytest.mark.asyncio
async def test_provider_resolution_limit_is_process_wide_across_multiplexers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    active = 0
    peak = 0
    counter_lock = threading.Lock()

    def delayed_prepare(profile_name: str):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(0.05)
            return SimpleNamespace(
                environment={
                    "AGENT_PROVIDER_PROFILE": profile_name,
                    "PROVIDER_CONFIGS": '{"synthetic-provider":{}}',
                },
                close=lambda: None,
            )
        finally:
            with counter_lock:
                active -= 1

    @contextlib.asynccontextmanager
    async def fake_stdio(_parameters, *, errlog):
        assert errlog is not None
        yield "read", "write"

    monkeypatch.setattr(
        "agent_utilities.core.provider_runtime.prepare_provider_runtime_child_environment",
        delayed_prepare,
    )
    monkeypatch.setattr(multiplexer_module, "stdio_client", fake_stdio)
    monkeypatch.setattr(multiplexer_module, "ClientSession", FakeStdioSession)
    multiplexers = [
        MCPMultiplexer(tmp_path / "unused-one.json"),
        MCPMultiplexer(tmp_path / "unused-two.json"),
    ]

    async def open_child(index: int) -> None:
        async with contextlib.AsyncExitStack() as stack:
            await multiplexers[index % 2]._open_one_session(
                f"synthetic-child-{index}",
                {
                    "command": "synthetic-child",
                    "provider_profile": "synthetic-provider",
                    "initialization_timeout": 2.0,
                },
                stack,
            )

    await asyncio.gather(*(open_child(index) for index in range(8)))

    assert 1 <= peak <= 4


@pytest.mark.asyncio
async def test_timed_out_provider_queue_stays_within_process_capacity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    release_workers = threading.Event()
    all_closed = threading.Event()
    counter_lock = threading.Lock()
    started = 0
    closed = 0
    submitted = 0

    def delayed_prepare(profile_name: str):
        nonlocal started
        with counter_lock:
            started += 1
        release_workers.wait(10.0)

        def close() -> None:
            nonlocal closed
            with counter_lock:
                closed += 1
                if closed == 8:
                    all_closed.set()

        return SimpleNamespace(
            environment={"AGENT_PROVIDER_PROFILE": profile_name},
            close=close,
        )

    original_submit = multiplexer_module._PROVIDER_RESOLUTION_EXECUTOR.submit

    def counted_submit(*args, **kwargs):
        nonlocal submitted
        with counter_lock:
            submitted += 1
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(
        "agent_utilities.core.provider_runtime.prepare_provider_runtime_child_environment",
        delayed_prepare,
    )
    monkeypatch.setattr(
        multiplexer_module._PROVIDER_RESOLUTION_EXECUTOR,
        "submit",
        counted_submit,
    )
    multiplexers = [
        MCPMultiplexer(tmp_path / "unused-one.json"),
        MCPMultiplexer(tmp_path / "unused-two.json"),
    ]

    async def time_out_child(index: int) -> None:
        async with contextlib.AsyncExitStack() as stack:
            with pytest.raises(RuntimeError, match="profile is unavailable"):
                await multiplexers[index % 2]._open_one_session(
                    f"synthetic-child-{index}",
                    {
                        "command": "synthetic-child",
                        "provider_profile": "synthetic-provider",
                        "initialization_timeout": 0.05,
                    },
                    stack,
                )

    try:
        await asyncio.gather(*(time_out_child(index) for index in range(8)))
        with counter_lock:
            assert started == 4
            assert submitted == 8

        # All eight process-wide slots remain owned by the four running and
        # four queued resolutions. Further timed-out callers must fail before
        # submitting more work items to ThreadPoolExecutor's unbounded queue.
        await asyncio.gather(*(time_out_child(index) for index in range(8, 24)))
        with counter_lock:
            assert submitted == 8
    finally:
        release_workers.set()

    assert await asyncio.to_thread(all_closed.wait, 2.0)
