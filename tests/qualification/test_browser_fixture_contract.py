"""Pure hygiene contracts only; these do not execute installed/native fixtures."""

import asyncio
import base64
import hashlib
import importlib.util
import json
import signal
import subprocess
import sys
from contextlib import contextmanager, nullcontext
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


@pytest.mark.parametrize(
    "fault",
    [None, "missing", "duplicate", "algorithm", "hash", "size", "no_hash", "no_size"],
)
def test_native_executable_must_match_wheel_record(controller, tmp_path, fault):
    binary = tmp_path / "server"
    binary.write_bytes(b"synthetic executable bytes")
    digest = hashlib.sha256(binary.read_bytes()).digest()
    entry = SimpleNamespace(
        hash=SimpleNamespace(
            mode="sha256", value=base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        ),
        size=binary.stat().st_size,
    )
    if fault == "algorithm":
        entry.hash.mode = "md5"
    elif fault == "hash":
        entry.hash.value = "mismatch"
    elif fault == "size":
        entry.size += 1
    elif fault == "no_hash":
        entry.hash = None
    elif fault == "no_size":
        entry.size = None
    files = (
        []
        if fault == "missing"
        else [entry, entry]
        if fault == "duplicate"
        else [entry]
    )
    distribution = SimpleNamespace(files=files, locate_file=lambda item: binary)
    if fault is None:
        assert (
            controller.native.verify_server_record(binary, distribution) == digest.hex()
        )
    else:
        with pytest.raises(RuntimeError, match="Native server wheel"):
            controller.native.verify_server_record(binary, distribution)


@pytest.mark.parametrize("version", ["2.27.7", "2.27.6", None])
def test_native_runtime_version_is_exact(controller, version):
    async def health():
        return {"version": version}

    client = SimpleNamespace(health=health)
    if version == "2.27.7":
        assert asyncio.run(controller.native.require_native_version(client)) == version
    else:
        with pytest.raises(RuntimeError, match="Native runtime version"):
            asyncio.run(controller.native.require_native_version(client))


@pytest.mark.parametrize("code", ["FORBIDDEN", "private-token-value", None])
def test_refusal_metadata_never_copies_unknown_codes_or_details(controller, code):
    exc = RuntimeError("private diagnostic detail")
    exc.code = code
    assert controller.safe_refusal_code(exc) == (
        "FORBIDDEN" if code == "FORBIDDEN" else None
    )


def test_prepare_failure_stage_reaches_public_receipt(
    controller, monkeypatch, tmp_path
):
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("QUALIFICATION_OUTPUT", str(tmp_path / "public"))
    monkeypatch.setenv("QUALIFICATION_VAULT_BINARY", "/unused-fixture-tool")
    monkeypatch.setattr(controller.native, "require_installed", lambda: {})
    monkeypatch.setattr(controller, "browser_ca", lambda path: (path, path, path))
    monkeypatch.setattr(controller, "configure_signer", lambda *args: "synthetic")
    monkeypatch.setitem(
        sys.modules,
        "arbora.security.secrets",
        SimpleNamespace(VaultBackend=lambda **kw: object()),
    )

    @contextmanager
    def infrastructure(*args):
        yield SimpleNamespace(url="https://fixture.invalid")

    @contextmanager
    def boot(**kwargs):
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        (config_dir / "config.json").write_text("{}")
        kwargs["before_serve"](
            {
                "env": {},
                "config_dir": config_dir,
                "issuer": SimpleNamespace(
                    _key=SimpleNamespace(as_dict=lambda **kw: {})
                ),
            }
        )
        yield

    async def refused(context, *args):
        context["fixture_step"] = "native_fixture_refusal"
        raise RuntimeError("private diagnostic must not escape")

    monkeypatch.setattr(controller, "vault", infrastructure)
    monkeypatch.setattr(
        controller, "https_origin", lambda *args: nullcontext("https://fixture.invalid")
    )
    monkeypatch.setattr(controller, "prepare", refused)
    monkeypatch.setattr(controller.boot, "_free_port", lambda: 12345, raising=False)
    monkeypatch.setattr(controller.boot, "ROLES", (), raising=False)
    monkeypatch.setattr(controller.boot, "_booted_stack", boot, raising=False)
    with pytest.raises(RuntimeError, match="Qualification failed"):
        controller.main()
    receipt = json.loads((tmp_path / "public" / "receipt.json").read_text())
    assert receipt["fixture_step"] == "native_fixture_refusal"
    assert "private diagnostic" not in json.dumps(receipt)
    assert not (tmp_path / "browser-private").exists()
