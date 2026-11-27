"""Advisory process locks that guard the local engine spawn (ARBORA-DEPLOY-R600)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
from types import SimpleNamespace

import pytest

from arbora.deployment import engine_lock, file_lock


@pytest.fixture
def pair(tmp_path):
    path = str(tmp_path / "lock")
    fds = [os.open(path, os.O_CREAT | os.O_RDWR, 0o644) for _ in range(2)]
    yield fds
    for fd in fds:
        os.close(fd)


@pytest.mark.spec("ARBORA-DEPLOY-R600.1")
def test_exclusive_lock_contends_then_frees(pair):
    a, b = pair
    file_lock.lock_exclusive_nb(a)
    with pytest.raises(file_lock.LockUnavailable):
        file_lock.lock_exclusive_nb(b)
    file_lock.unlock(a)
    file_lock.unlock(a)  # idempotent
    file_lock.lock_exclusive_nb(b)


@pytest.mark.spec("ARBORA-DEPLOY-R600.1")
def test_shared_locks_coexist_but_exclude_exclusive(pair):
    a, b = pair
    file_lock.lock_shared_nb(a)
    file_lock.lock_shared_nb(b)
    file_lock.unlock(b)
    with pytest.raises(file_lock.LockUnavailable):
        file_lock.lock_exclusive_nb(b)


@pytest.mark.spec("ARBORA-DEPLOY-R600")
@pytest.mark.spec("ARBORA-DEPLOY-R600.2")
def test_engine_guard_is_per_socket(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    a, b = (str(tmp_path / f"{n}.sock") for n in "ab")
    assert engine_lock.engine_lock_path(a) != engine_lock.engine_lock_path(b)
    with engine_lock.engine_spawn_guard(a, timeout=0.5) as first:
        assert first is True
        holder = engine_lock.engine_lock_holder(a)
        assert holder["pid"] == os.getpid() and "socket" not in holder
        with engine_lock.engine_spawn_guard(b, timeout=0.5) as other:
            assert other is True
        with engine_lock.engine_spawn_guard(a, timeout=0.1) as second:
            assert second is False
    with engine_lock.engine_spawn_guard(a, timeout=0.5) as again:
        assert again is True


@pytest.mark.spec("ARBORA-DEPLOY-R600.1")
def test_lock_path_is_a_short_digest_with_no_host_or_socket_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    transport = "/run/private/engine.sock"
    path = engine_lock.engine_lock_path(transport)
    digest = hashlib.sha256(transport.encode()).hexdigest()[:8]
    assert path.name == f"engine-{digest}.lock"
    assert "private" not in str(path.name) and socket.gethostname() not in path.name


@pytest.mark.spec("ARBORA-DEPLOY-R600.5")
def test_lock_file_holds_pid_and_opaque_digest_only(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    transport = str(tmp_path / "engine.sock")
    with engine_lock.engine_spawn_guard(transport, timeout=0.5) as acquired:
        assert acquired is True
        text = engine_lock.engine_lock_path(transport).read_text()
    record = json.loads(text)
    assert set(record) == {"pid", "acquired_at", "transport_digest"}
    assert record["pid"] == os.getpid()
    assert re.fullmatch(r"[0-9a-f]{16}", record["transport_digest"])
    assert (
        record["transport_digest"]
        == hashlib.sha256(transport.encode()).hexdigest()[:16]
    )
    assert transport not in text and socket.gethostname() not in text


@pytest.mark.spec("ARBORA-DEPLOY-R600.6")
def test_engine_guard_acquire_time_budget(tmp_path, monkeypatch):
    """An uncontended acquire never waits; a contended one gives up on the budget."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    slept: list[float] = []
    now = [0.0]

    def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(engine_lock.time, "sleep", fake_sleep)
    monkeypatch.setattr(engine_lock.time, "monotonic", lambda: now[0])
    transport = str(tmp_path / "budget.sock")
    with engine_lock.engine_spawn_guard(transport, timeout=30.0) as first:
        assert first is True and slept == []
        with engine_lock.engine_spawn_guard(transport, timeout=1.0) as busy:
            assert busy is False
    # One second of budget polls at the guard's own interval and stops there.
    assert slept and sum(slept) == pytest.approx(1.0, abs=0.2)
    assert len(slept) <= 6


@pytest.mark.spec("ARBORA-DEPLOY-R600.4")
def test_busy_guard_refuses_without_spawning(tmp_path, monkeypatch):
    from arbora.mcp_server import composition
    from arbora.mcp_server.composition import LocalEngineConfig
    from arbora.mcp_server.served_engine import EngineAttachments

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    config = LocalEngineConfig(
        port=47999, persist_dir=tmp_path / "store", extra_args=()
    )
    address = config.endpoint.address
    spawned: list[str] = []
    monkeypatch.setattr(
        composition,
        "probe_engine",
        lambda *_a, **_k: SimpleNamespace(state="absent"),
    )
    monkeypatch.setattr(
        composition, "_spawn_engine", lambda *_a, **_k: spawned.append("spawn")
    )
    original = engine_lock.engine_spawn_guard
    monkeypatch.setattr(
        engine_lock,
        "engine_spawn_guard",
        lambda key, timeout=0.1: original(key, timeout=0.05),
    )
    with original(address, timeout=0.5) as held:
        assert held is True
        with pytest.raises(composition.EngineRefusal) as refused:
            composition.ensure_local_engine(config, EngineAttachments())
    assert refused.value.code == "ENGINE_SPAWN_GUARD_BUSY"
    assert str(os.getpid()) in str(refused.value)
    assert spawned == []
    assert not engine_lock.engine_marker_path(address).exists()
