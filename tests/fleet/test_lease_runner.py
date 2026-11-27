"""``arbora lease run``: the run machine, the heartbeat, the release and the child's isolation.

Rows ARBORA-CAPACITY-R754.3 to R754.6. The engine is the recorded stand-in of
``tests/fleet/_lease_engine.py``; the children are real processes. Time is waited on with
events and bounded polls, never a fixed sleep.
"""

from __future__ import annotations

import itertools
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from arbora.fleet import lease_runner
from arbora.fleet.lease_runner import (
    RUN_EDGES,
    LeaseRun,
    LeaseRunMachine,
    RunEvent,
    RunState,
    RunTransitionRefused,
)
from tests.fleet._lease_engine import EngineDoor, FakeEngine, serve

WAIT_S = 20.0


def _run(engine: FakeEngine, argv: list[str], **kw: Any) -> LeaseRun:
    run = LeaseRun(
        EngineDoor(engine),
        pool="cell-a",
        priority="lane",
        argv=argv,
        ttl_ms=kw.pop("ttl_ms", 300),
        poll_s=0.05,
        kill_grace_s=kw.pop("kill_grace_s", 5.0),
        **kw,
    )
    return run


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)  # a bounded poll for another process to die
    return False


def _pid(path: Path) -> int:
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        text = path.read_text() if path.exists() else ""
        if text.strip():
            return int(text)
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written")


@pytest.mark.spec("ARBORA-CAPACITY-R754.3")
def test_every_legal_run_edge_applies_and_every_other_pair_is_refused() -> None:
    pairs = list(itertools.product(RunState, RunEvent))
    assert len(pairs) == 63
    assert len(RUN_EDGES) == 14
    legal = 0
    for state, event in pairs:
        machine = LeaseRunMachine(state)
        if (state, event) in RUN_EDGES:
            assert machine.apply(event) is RUN_EDGES[(state, event)]
            legal += 1
            continue
        with pytest.raises(RunTransitionRefused) as refused:
            machine.apply(event)
        assert (refused.value.from_state, refused.value.event) == (state, event)
        assert machine.state is state
    assert legal == 14
    assert LeaseRunMachine().state is RunState.ACQUIRING


@pytest.mark.spec("ARBORA-CAPACITY-R754.4")
def test_a_revoked_lease_kills_the_child_group_and_exits_75(tmp_path: Path) -> None:
    engine, pids = FakeEngine(), tmp_path / "grandchild.pid"
    engine.revoke_at_renew = 3
    script = f"sleep 300 & echo $! > {pids}; wait"
    started = time.monotonic()
    code = _run(engine, ["sh", "-c", script]).run()
    assert code == 75
    # The grace is 5 s here; the group died on SIGTERM, well inside it.
    assert time.monotonic() - started < 5.0
    assert _gone(_pid(pids))
    assert engine.renews["lease-1"] == 3
    assert [verb for verb, _ in engine.calls].count("release") == 1


@pytest.mark.spec("ARBORA-CAPACITY-R754.5")
def test_release_runs_on_exit_on_signal_and_when_a_grandchild_outlives_the_child(
    tmp_path: Path,
) -> None:
    exited = FakeEngine()
    assert _run(exited, ["sh", "-c", "exit 7"]).run() == 7
    assert exited.calls[-1][0] == "release"
    assert [v for v, _ in exited.calls].count("release") == 1

    signalled = FakeEngine()
    run = _run(signalled, ["sleep", "300"])
    watcher = threading.Thread(target=_terminate_when_running, args=(signalled,))
    watcher.start()
    assert run.run() == 128 + signal.SIGTERM
    watcher.join()
    assert [v for v, _ in signalled.calls].count("release") == 1

    outlived, pids = FakeEngine(), tmp_path / "background.pid"
    seen: list[bool] = []
    outlived.on_release = lambda _id: seen.append(_alive(_pid(pids)))
    assert _run(outlived, ["sh", "-c", f"sleep 300 & echo $! > {pids}"]).run() == 0
    assert seen == [True]  # released while the background process still ran
    os.kill(_pid(pids), signal.SIGKILL)
    assert [v for v, _ in outlived.calls].count("release") == 1


def _alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _terminate_when_running(engine: FakeEngine) -> None:
    assert engine.wait_for(
        lambda e: any(x["state"] == "running" for x in e.leases.values()), WAIT_S
    )
    os.kill(os.getpid(), signal.SIGTERM)


CHILD = """
import json, os, sys, time
out, go = sys.argv[1], sys.argv[2]
links = []
for n in os.listdir("/proc/self/fd"):
    try:
        links.append(os.readlink(f"/proc/self/fd/{n}"))
    except FileNotFoundError:
        pass
with open(out, "w") as handle:
    handle.write(json.dumps({"env": dict(os.environ), "fds": links}))
deadline = time.monotonic() + 30
while not os.path.exists(go) and time.monotonic() < deadline:
    time.sleep(0.02)
"""


def _command(
    url: str, credential: str, work: Path, name: str, go: Path
) -> subprocess.Popen[bytes]:
    env = {
        **os.environ,
        "ARBORA_URL": url,
        "ARBORA_" + "TOKEN": credential,
    }
    child = ["--", sys.executable, "-c", CHILD, str(work / f"{name}.out"), str(go)]
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "arbora.fleet.lease_runner",
            "--pool",
            "cell-a",
            "--priority",
            "lane",
            "--ttl-ms",
            "600",
            "--poll-secs",
            "0.05",
            *child,
        ],
        env=env,
    )


def _running(engine: FakeEngine) -> bool:
    return engine.wait_for(
        lambda e: any(x["state"] == "running" for x in e.leases.values()), WAIT_S
    )


def _two_commands_share_one_slot(
    engine: FakeEngine, url: str, credential: str, work: Path
) -> None:
    """The second command queues behind the first, which heartbeats, exits and frees it."""
    go_first, go_second = work / "go1", work / "go2"
    first = _command(url, credential, work, "first", go_first)
    second = None
    try:
        assert _running(engine)
        second = _command(url, credential, work, "second", go_second)
        assert engine.wait_for(lambda e: "lease-2" in e.leases, WAIT_S)
        assert engine.leases["lease-2"]["state"] == "queued"
        assert engine.wait_for(lambda e: e.renews.get("lease-1", 0) >= 1, WAIT_S)
        go_first.touch()
        assert first.wait(timeout=WAIT_S) == 0
        assert engine.wait_for(
            lambda e: e.leases["lease-2"]["state"] in {"granted", "running"}, WAIT_S
        )
        go_second.touch()
        assert second.wait(timeout=WAIT_S) == 0
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()


def _assert_no_lease_handle(report: dict[str, Any], handle: set[str]) -> None:
    env = report["env"]
    assert not [n for n in env if n.upper().startswith("ARBORA_")]
    assert not [v for v in env.values() if v in handle]
    assert not [
        n for n in env if any(w in n.upper() for w in lease_runner._SECRET_WORDS)
    ]
    assert not [link for link in report["fds"] if link.startswith("socket:")]


@pytest.mark.spec("ARBORA-CAPACITY-R754.6")
def test_runner_holds_heartbeats_kills_on_revoke_and_releases(tmp_path: Path) -> None:
    engine, credential = FakeEngine(), secrets.token_hex(8)
    server = serve(engine, credential)
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}"
        _two_commands_share_one_slot(engine, url, credential, tmp_path)
    finally:
        server.shutdown()
    report = json.loads((tmp_path / "first.out").read_text())
    handle = {"lease-1", str(engine.leases["lease-1"]["fence_token"]), credential}
    _assert_no_lease_handle(report, handle)
    assert engine.leases["lease-1"]["state"] == "released"
    assert engine.leases["lease-2"]["state"] == "released"


@pytest.mark.spec("ARBORA-CAPACITY-R754.3")
def test_scrubbed_environment_drops_lease_and_credential_names() -> None:
    env = {
        "PATH": "/bin",
        "ARBORA_URL": "u",
        "MY_API_KEY": "k",
        "Db_Password": "p",
        "X": "lease-9",
    }
    assert lease_runner._scrub_env(env, ["lease-9"]) == {"PATH": "/bin"}


@pytest.mark.spec("ARBORA-CAPACITY-R754.3")
def test_console_routes_lease_run_before_the_usage_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from arbora import console

    seen: list[list[str]] = []

    def fake_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 42

    monkeypatch.setattr(lease_runner, "main", fake_main)
    monkeypatch.setattr("arbora.security.error_surface.bind_errors", lambda _e: None)
    argv = [
        "arbora",
        "lease",
        "run",
        "--pool",
        "c",
        "--priority",
        "lane",
        "--",
        "make",
        "--help",
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exit_:
        console.main()
    assert exit_.value.code == 42
    assert seen == [argv[3:]]  # the child's own --help is not the console's
    assert main_usage_lists_the_command()


def main_usage_lists_the_command() -> bool:
    import contextlib
    import io

    from arbora.mcp_server import static_answers

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        static_answers.print_usage_or_version(["--help"])
    return "arbora lease run" in out.getvalue()
