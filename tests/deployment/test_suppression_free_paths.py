"""Paths that carried a coverage pragma are reached by tests (ARBORA-HOST-R039.12)."""

from __future__ import annotations

import ctypes
import importlib.util
import runpy
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from arbora.deployment import engine_lock, file_lock, self_deploy

MODULES = [
    "arbora.deployment.cli",
    "arbora.deployment.doctor",
    "arbora.deployment.venv_sync",
    "arbora.deployment.acceptance.runner",
    "arbora.deployment.deployed_source",
]


@pytest.mark.parametrize("module", MODULES)
def test_module_entry_guard_runs_main(
    module: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "argv", [module, "--help"])
    monkeypatch.delitem(sys.modules, module, raising=False)
    with pytest.raises(SystemExit) as stop:
        runpy.run_module(module, run_name="__main__")
    assert stop.value.code == 0


def test_runtime_base_falls_back_to_the_xdg_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setitem(sys.modules, "platformdirs", None)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert engine_lock._runtime_base() == tmp_path / "arbora"
    assert (tmp_path / "arbora").is_dir()


def test_engine_lock_holder_is_none_for_an_unreadable_or_malformed_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record = tmp_path / "holder.lock"
    monkeypatch.setattr(engine_lock, "engine_lock_path", lambda _key: record)
    assert engine_lock.engine_lock_holder(None) is None
    record.write_text("{not json")
    assert engine_lock.engine_lock_holder(None) is None
    record.write_text('{"pid": 1}')
    assert engine_lock.engine_lock_holder(None) == {"pid": 1}


class _Policies:
    def for_engine(self, _engine: Any) -> Any:
        return types.SimpleNamespace(decide=lambda _request: "allow")


def _redeploy(**kwargs: Any) -> dict[str, Any]:
    return self_deploy.execute_redeploy(
        action_policies=_Policies(), confirm=True, **kwargs
    )


def test_a_raising_restart_reports_failed_with_a_logged_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def restart(_service: str) -> None:
        raise RuntimeError("restart exploded")

    result = _redeploy(restart_fn=restart)
    assert result["status"] == "failed"
    assert result["stage"] == "restart"
    assert any(r.exc_info for r in caplog.records)


def test_a_raising_health_check_reports_unhealthy_with_a_logged_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unhealthy(_service: str) -> bool:
        raise RuntimeError("health exploded")

    result = _redeploy(restart_fn=lambda _s: None, health_fn=unhealthy)
    assert result["status"] == "unhealthy"
    assert result["healthy"] is False
    assert any(r.exc_info for r in caplog.records)


class _Kernel32:
    """A stand-in for the Win32 locking API: one byte-range lock at a time."""

    def __init__(self) -> None:
        self.held = False
        self.error = 0

        def lock_file_ex(_h: Any, flags: int, *_rest: Any) -> int:
            if self.held:
                self.error = 33 if flags & 0x1 else 5
                return 0
            self.held = True
            return 1

        def unlock_file_ex(*_args: Any) -> int:
            self.held = False
            return 1

        self.LockFileEx = lock_file_ex
        self.UnlockFileEx = unlock_file_ex


def _load_windows_lock(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, _Kernel32]:
    kernel = _Kernel32()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_a, **_k: kernel, raising=False)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: kernel.error, raising=False)
    monkeypatch.setattr(
        ctypes,
        "WinError",
        lambda code: types.SimpleNamespace(strerror=f"error {code}"),
        raising=False,
    )
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: fd)
    )
    spec = importlib.util.spec_from_file_location(
        "arbora_file_lock_windows", file_lock.__file__
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, kernel


def test_the_windows_lock_branch_locks_contends_and_unlocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, kernel = _load_windows_lock(monkeypatch)
    assert module.lock_exclusive(3) is True
    assert module.lock_exclusive(3, blocking=False) is False
    with pytest.raises(module.LockUnavailable):
        module.lock_exclusive_nb(3)
    with pytest.raises(module.LockUnavailable):
        module.lock_shared_nb(3)
    assert module.lock_shared(3, blocking=False) is False
    kernel.error = 5
    with pytest.raises(OSError):
        module.lock_shared(3)
    module.unlock(3)
    assert kernel.held is False
    assert module.lock_shared(3) is True
    module.unlock(3)
    module.lock_exclusive_nb(3)
    module.unlock(3)
    module.lock_shared_nb(3)
