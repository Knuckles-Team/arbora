"""Advisory process locks and the transport drain gate."""

from __future__ import annotations

import os

import pytest

from arbora.deployment import engine_lock, file_lock, host_lock


@pytest.fixture
def pair(tmp_path):
    path = str(tmp_path / "lock")
    fds = [os.open(path, os.O_CREAT | os.O_RDWR, 0o644) for _ in range(2)]
    yield fds
    for fd in fds:
        os.close(fd)


def test_exclusive_lock_contends_then_frees(pair):
    a, b = pair
    file_lock.lock_exclusive_nb(a)
    with pytest.raises(file_lock.LockUnavailable):
        file_lock.lock_exclusive_nb(b)
    file_lock.unlock(a)
    file_lock.unlock(a)  # idempotent
    file_lock.lock_exclusive_nb(b)


def test_shared_locks_coexist_but_exclude_exclusive(pair):
    a, b = pair
    file_lock.lock_shared_nb(a)
    file_lock.lock_shared_nb(b)
    file_lock.unlock(b)
    with pytest.raises(file_lock.LockUnavailable):
        file_lock.lock_exclusive_nb(b)


@pytest.fixture
def fresh_host(tmp_path, monkeypatch):
    monkeypatch.setenv("ARBORA_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("ARBORA_TESTING", raising=False)
    monkeypatch.delenv("KG_DAEMON_ROLE", raising=False)
    monkeypatch.setattr(host_lock, "_effective_role", None, raising=False)
    monkeypatch.setattr(host_lock, "_lock_fd", None, raising=False)
    yield host_lock
    host_lock.release_host_lock()


def test_client_role_takes_no_lock(fresh_host):
    assert fresh_host.resolve_daemon_role("client") == "client"
    assert fresh_host.is_host() is False


def test_host_acquires_reports_holder_and_releases(fresh_host):
    assert fresh_host.host_daemon_running() is False
    assert fresh_host.resolve_daemon_role("host") == "host"
    assert fresh_host.host_lock_holder()["pid"] == os.getpid()
    assert fresh_host.host_daemon_running() is True
    fresh_host.release_host_lock()
    assert fresh_host.host_daemon_running() is False


def test_second_host_blocks_and_auto_falls_back_to_client(fresh_host):
    path = fresh_host._lock_path()
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        file_lock.lock_exclusive_nb(fd)
        assert fresh_host.resolve_daemon_role("auto") == "client"
        fresh_host._effective_role = None
        with pytest.raises(fresh_host.KGHostAlreadyRunning):
            fresh_host.resolve_daemon_role("host")
    finally:
        os.close(fd)


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
