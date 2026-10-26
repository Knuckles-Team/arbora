"""Pure hygiene contracts only; these do not execute installed/native fixtures."""

import importlib.util
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.spec("ARBORA-IDENTITY-R004.5.2")


@pytest.fixture
def controller(monkeypatch):
    path = Path(__file__).parent / "browser" / "qualify.py"
    # Do not import or execute the process-boot helper in these pure contracts.
    monkeypatch.setitem(sys.modules, "test_real_boot", SimpleNamespace())
    original = list(sys.path)
    spec = importlib.util.spec_from_file_location("fixture_controller_contract", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.path[:] = original


def test_browser_environment_never_inherits_credentials(
    controller, monkeypatch, tmp_path
):
    for name in (
        "QUALIFICATION_BROKER_TOKEN",
        "VAULT_TOKEN",
        "GITHUB_TOKEN",
        "DEBUG",
        "PYTHONPATH",
    ):
        monkeypatch.setenv(name, "fixture-private-value")
    monkeypatch.setenv("PATH", "/fixture/bin")
    env = controller.browser_environment(tmp_path, tmp_path / "ca.pem")
    assert set(env) <= {
        "PATH",
        "LANG",
        "LC_ALL",
        "PLAYWRIGHT_BROWSERS_PATH",
        "HOME",
        "QUALIFICATION_BROWSER_HOME",
        "NODE_EXTRA_CA_CERTS",
        "TMPDIR",
    }
    assert "fixture-private-value" not in env.values()
    assert env["HOME"] == str(tmp_path / "home")


def test_exited_browser_leader_still_reaps_owned_group(controller, monkeypatch):
    calls = []
    monkeypatch.setattr(
        controller.os, "killpg", lambda pid, action: calls.append((pid, action))
    )
    process = SimpleNamespace(pid=123, poll=lambda: 0, wait=lambda **kw: 0)
    controller.terminate(process)
    assert calls == [(123, signal.SIGTERM), (123, signal.SIGKILL)]


def test_timed_out_browser_is_killed(controller, monkeypatch):
    calls = []

    def wait(**kwargs):
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("fixture", 5)
        return 0

    monkeypatch.setattr(
        controller.os, "killpg", lambda pid, action: calls.append(action)
    )
    controller.terminate(SimpleNamespace(pid=123, wait=wait))
    assert calls == [signal.SIGTERM, signal.SIGKILL]


def test_private_files_removed_even_when_termination_fails(
    controller, monkeypatch, tmp_path
):
    private = tmp_path / "private"
    private.mkdir()
    (private / "fixture.key").write_text("synthetic-private-placeholder")
    monkeypatch.setenv("QUALIFICATION_BROKER_TOKEN", "synthetic-private-placeholder")

    def fail(process):
        raise OSError("fixture termination failed")

    monkeypatch.setattr(controller, "terminate", fail)
    with pytest.raises(OSError):
        controller.cleanup(private, object())
    assert not private.exists()
    assert "QUALIFICATION_BROKER_TOKEN" not in controller.os.environ


def test_missing_process_group_is_safe(controller, monkeypatch):
    def absent(*args):
        raise ProcessLookupError

    monkeypatch.setattr(controller.os, "killpg", absent)
    controller.terminate(SimpleNamespace(pid=123, wait=lambda **kw: 0))
