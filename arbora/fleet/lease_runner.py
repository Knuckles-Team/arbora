"""``arbora lease run``: hold one build-slot lease around one child process (ARBORA-CAPACITY-R754.3 to .6).

The command asks the served ``capacity.lease.*`` operations over HTTP for a lease, runs its
argument vector as a child in a new process group while the lease is held, renews the lease
on its own thread, and releases it on every way out. The engine owns the pool, the queue and
every lease transition; this module holds no pool or lease state beyond the one fence it was
granted. Its own lifecycle is the explicit machine :class:`LeaseRunMachine`.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import signal
import subprocess  # nosec B404 - the operator's own argument vector, no shell
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from types import MappingProxyType
from typing import Any, Protocol

import httpx

__all__ = [
    "RUN_EDGES",
    "DoorRefused",
    "DoorUnreachable",
    "HttpDoor",
    "LeaseRun",
    "LeaseRunMachine",
    "RunEvent",
    "RunState",
    "RunTransitionRefused",
    "main",
]

#: A renew goes out every ``ttl / RENEW_DIVISOR`` while the child runs (R754.4).
RENEW_DIVISOR = 3
#: SIGTERM to the child's group, then SIGKILL after this many seconds (R754.4).
KILL_GRACE_S = 10.0
#: The release is retried within this budget and then given up (R754.5).
RELEASE_BUDGET_S = 5.0
RELEASE_ATTEMPTS = 3
POLL_S = 2.0
DEFAULT_WAIT_S = 7200
DEFAULT_TTL_MS = 60_000
EXIT_LEASE_LOST = 75
EXIT_REFUSED = 69
EXIT_USAGE = 64
_DOOR_TIMEOUT_S = 10.0
_LOST_STATES = frozenset({"released", "expired", "revoked"})
_SECRET_WORDS = ("TOKEN", "SECRET", "KEY", "PASSWORD")
_FORWARDED = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


class RunState(enum.Enum):
    ACQUIRING = "Acquiring"
    WAITING = "Waiting"
    STARTING = "Starting"
    RUNNING = "Running"
    STOPPING = "Stopping"
    RELEASING = "Releasing"
    DONE = "Done"


class RunEvent(enum.Enum):
    GRANTED = "Granted"
    QUEUED = "Queued"
    START_ACCEPTED = "StartAccepted"
    SPAWN_FAILED = "SpawnFailed"
    CHILD_EXITED = "ChildExited"
    SIGNAL = "Signal"
    LEASE_LOST = "LeaseLost"
    ACQUIRE_REFUSED = "AcquireRefused"
    RELEASE_DONE = "ReleaseDone"


_S, _E = RunState, RunEvent
#: The 14 legal edges A01 to A14; every other pair is refused.
RUN_EDGES: Mapping[tuple[RunState, RunEvent], RunState] = MappingProxyType(
    {
        (_S.ACQUIRING, _E.QUEUED): _S.WAITING,  # A01
        (_S.ACQUIRING, _E.GRANTED): _S.STARTING,  # A02
        (_S.WAITING, _E.GRANTED): _S.STARTING,  # A03
        (_S.WAITING, _E.SIGNAL): _S.RELEASING,  # A04
        (_S.WAITING, _E.LEASE_LOST): _S.DONE,  # A05
        (_S.ACQUIRING, _E.ACQUIRE_REFUSED): _S.DONE,  # A06
        (_S.STARTING, _E.START_ACCEPTED): _S.RUNNING,  # A07
        (_S.STARTING, _E.SPAWN_FAILED): _S.RELEASING,  # A08
        (_S.STARTING, _E.SIGNAL): _S.RELEASING,  # A09
        (_S.RUNNING, _E.LEASE_LOST): _S.STOPPING,  # A10
        (_S.RUNNING, _E.SIGNAL): _S.STOPPING,  # A11
        (_S.RUNNING, _E.CHILD_EXITED): _S.RELEASING,  # A12
        (_S.STOPPING, _E.CHILD_EXITED): _S.RELEASING,  # A13
        (_S.RELEASING, _E.RELEASE_DONE): _S.DONE,  # A14
    }
)


class RunTransitionRefused(Exception):
    """An event the run machine does not accept in its current state."""

    code = "RUN_TRANSITION_REFUSED"

    def __init__(self, from_state: RunState, event: RunEvent) -> None:
        self.from_state = from_state
        self.event = event
        super().__init__(f"{self.code}: from={from_state.value} event={event.value}")


class LeaseRunMachine:
    """The run lifecycle: named states, events and edges; no flags."""

    def __init__(self, state: RunState = RunState.ACQUIRING) -> None:
        self.state = state

    def apply(self, event: RunEvent) -> RunState:
        try:
            self.state = RUN_EDGES[(self.state, event)]
        except KeyError:
            raise RunTransitionRefused(self.state, event) from None
        return self.state


class DoorRefused(Exception):
    """The served operation refused the call with its own code."""

    def __init__(self, code: str, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(code)


class DoorUnreachable(Exception):
    """The door did not answer in time, or answered something unreadable."""


class Door(Protocol):
    def call(
        self, op_id: str, params: Mapping[str, Any], *, key: str | None = None
    ) -> Mapping[str, Any]: ...


class HttpDoor:
    """The served operations over HTTP with the caller's configured bearer."""

    def __init__(
        self,
        base_url: str,
        credential: str,
        *,
        timeout_s: float = _DOOR_TIMEOUT_S,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.startswith(("http://", "https://")) or not credential:
            raise ValueError("an absolute http(s) URL and a credential are required")
        self._base = base_url.rstrip("/")
        self._credential = credential
        self._timeout_s = timeout_s
        self._transport = transport

    def call(
        self, op_id: str, params: Mapping[str, Any], *, key: str | None = None
    ) -> Mapping[str, Any]:
        headers = {"Authorization": f"Bearer {self._credential}"}
        if key:
            headers["Idempotency-Key"] = key
        try:
            with httpx.Client(
                timeout=self._timeout_s, transport=self._transport
            ) as client:
                response = client.post(
                    f"{self._base}/api/v1/ops/{op_id}",
                    json=dict(params),
                    headers=headers,
                )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise DoorUnreachable(type(exc).__name__) from None
        if not isinstance(payload, Mapping):
            raise DoorUnreachable("unreadable answer")
        error = payload.get("error")
        if payload.get("ok") is False or isinstance(error, Mapping):
            detail = error if isinstance(error, Mapping) else {}
            raise DoorRefused(str(detail.get("code") or "UNAVAILABLE"), detail)
        result = payload.get("result", payload)
        value = result.get("value") if isinstance(result, Mapping) else None
        if not isinstance(value, Mapping):
            raise DoorUnreachable("unreadable answer")
        return value


def _scrub_env(env: Mapping[str, str], handle: Iterable[str] = ()) -> dict[str, str]:
    """The child's environment: no ``ARBORA_*``, no secret-named variable, no lease value."""
    held = {text for text in handle if text}
    kept: dict[str, str] = {}
    for name, value in env.items():
        upper = name.upper()
        if upper.startswith("ARBORA_") or any(w in upper for w in _SECRET_WORDS):
            continue
        if value in held:
            continue
        kept[name] = value
    return kept


def _spawn_child(
    argv: Sequence[str], env: Mapping[str, str], handle: Iterable[str] = ()
) -> subprocess.Popen[bytes]:
    """Start the child in its own process group with no descriptor of the runner's."""
    return subprocess.Popen(  # noqa: S603  # nosec B603 - the operator's own argv, no shell
        list(argv),
        close_fds=True,
        pass_fds=(),
        start_new_session=True,
        env=_scrub_env(env, handle),
    )


def _fence(lease: Mapping[str, Any], ttl_ms: int) -> dict[str, Any]:
    return {
        "lease_id": lease["lease_id"],
        "lease_epoch": lease["lease_epoch"],
        "fence_token": lease["fence_token"],
        "ttl_ms": ttl_ms,
    }


def _heartbeat_loop(
    door: Door,
    lease: Mapping[str, Any],
    ttl_ms: int,
    stop: threading.Event,
    lost: threading.Event,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Renew every ttl/3 until stopped; fire ``lost`` when the lease cannot be renewed."""
    interval = ttl_ms / 1000 / RENEW_DIVISOR
    last_ok = clock()
    failures = 0
    try:
        while not stop.wait(interval):
            try:
                door.call(
                    "capacity.lease.renew",
                    _fence(lease, ttl_ms),
                    key=uuid.uuid4().hex,
                )
                last_ok, failures = clock(), 0
            except DoorRefused as refused:
                if refused.code == "TRANSITION_REFUSED":
                    lost.set()
                    return
                failures += 1
            except DoorUnreachable:
                failures += 1
            if failures >= 3 and clock() - last_ok >= ttl_ms / 1000:
                lost.set()
                return
    except BaseException:
        lost.set()
        raise


class LeaseRun:
    """One ``arbora lease run``: acquire, wait, start, supervise, stop, release."""

    def __init__(
        self,
        door: Door,
        *,
        pool: str,
        priority: str,
        argv: Sequence[str],
        wait_secs: float = DEFAULT_WAIT_S,
        ttl_ms: int = DEFAULT_TTL_MS,
        poll_s: float = POLL_S,
        kill_grace_s: float = KILL_GRACE_S,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.machine = LeaseRunMachine()
        self.exit_code = 0
        self._door, self._pool, self._priority = door, pool, priority
        self._argv, self._wait_s, self._ttl_ms = list(argv), wait_secs, ttl_ms
        self._poll_s, self._grace_s = poll_s, kill_grace_s
        self._env = dict(os.environ if env is None else env)
        self._clock = clock
        self._lease: dict[str, Any] = {}
        self._child: subprocess.Popen[bytes] | None = None
        self._wake = threading.Event()
        self._signum = 0
        self._beat_stop = threading.Event()
        self._lost = threading.Event()
        self._beat: threading.Thread | None = None
        self._forward = signal.SIGTERM
        self._handlers = {
            RunState.ACQUIRING: self._acquiring,
            RunState.WAITING: self._waiting,
            RunState.STARTING: self._starting,
            RunState.RUNNING: self._running,
            RunState.STOPPING: self._stopping,
            RunState.RELEASING: self._releasing,
        }

    def run(self) -> int:
        """Drive the machine to Done and return the exit code."""
        previous = self._install_signals()
        try:
            while self.machine.state is not RunState.DONE:
                self.machine.apply(self._handlers[self.machine.state]())
        finally:
            self._reap_stray_child()
            self._restore_signals(previous)
        return self.exit_code

    def _install_signals(self) -> dict[int, Any]:
        if threading.current_thread() is not threading.main_thread():
            return {}
        return {s: signal.signal(s, self._on_signal) for s in _FORWARDED}

    @staticmethod
    def _restore_signals(previous: Mapping[int, Any]) -> None:
        for number, handler in previous.items():
            signal.signal(number, handler)

    def _on_signal(self, number: int, _frame: Any) -> None:
        self._signum = self._signum or number
        self._wake.set()

    def _signalled(self) -> RunEvent:
        self.exit_code = 128 + self._signum
        return RunEvent.SIGNAL

    def _acquiring(self) -> RunEvent:
        try:
            value = self._door.call(
                "capacity.lease.request",
                {
                    "cell_id": self._pool,
                    "priority": self._priority,
                    "work_item_id": f"lease-run-{uuid.uuid4().hex}",
                    "ttl_ms": self._ttl_ms,
                },
                key=uuid.uuid4().hex,
            )
            self._lease = dict(value["lease"])
            state = self._lease.get("state")
        except (DoorRefused, DoorUnreachable, KeyError, TypeError):
            self.exit_code = EXIT_REFUSED
            return RunEvent.ACQUIRE_REFUSED
        if state in {"granted", "queued"}:
            return RunEvent.GRANTED if state == "granted" else RunEvent.QUEUED
        self.exit_code = EXIT_REFUSED
        return RunEvent.ACQUIRE_REFUSED

    def _waiting(self) -> RunEvent:
        deadline = self._clock() + self._wait_s
        while not self._signum:
            state = self._poll_state()
            if state == "granted":
                return RunEvent.GRANTED
            if state in _LOST_STATES or state == "absent":
                self.exit_code = EXIT_LEASE_LOST
                return RunEvent.LEASE_LOST
            if self._clock() >= deadline:
                self.exit_code = EXIT_REFUSED
                return RunEvent.SIGNAL
            self._wake.wait(self._poll_s)
        return self._signalled()

    def _poll_state(self) -> str:
        """The lease's state now; ``unknown`` while the door cannot say."""
        try:
            value = self._door.call(
                "capacity.lease.get", {"lease_id": self._lease["lease_id"]}
            )
        except DoorRefused as refused:
            return "absent" if refused.code == "NOT_FOUND" else "unknown"
        except DoorUnreachable:
            return "unknown"
        self._lease = dict(value.get("lease") or self._lease)
        return str(self._lease.get("state"))

    def _starting(self) -> RunEvent:
        if self._signum:
            return self._signalled()
        handle = (str(self._lease["lease_id"]), str(self._lease["fence_token"]))
        try:
            self._child = _spawn_child(self._argv, self._env, handle)
        except OSError as error:
            self.exit_code = 127 if isinstance(error, FileNotFoundError) else 126
            return RunEvent.SPAWN_FAILED
        try:
            self._door.call(
                "capacity.lease.start",
                _fence(self._lease, self._ttl_ms),
                key=uuid.uuid4().hex,
            )
        except (DoorRefused, DoorUnreachable) as error:
            lost = isinstance(error, DoorRefused) and error.code == "TRANSITION_REFUSED"
            self.exit_code = EXIT_LEASE_LOST if lost else EXIT_REFUSED
            self._kill_group()
            return RunEvent.SPAWN_FAILED
        self._beat = threading.Thread(
            target=_heartbeat_loop,
            args=(self._door, self._lease, self._ttl_ms, self._beat_stop, self._lost),
            daemon=True,
        )
        self._beat.start()
        return RunEvent.START_ACCEPTED

    def _running(self) -> RunEvent:
        child = self._child
        assert child is not None  # Running is entered only after a spawn
        while True:
            try:
                code = child.wait(timeout=min(self._poll_s, 0.1))
            except subprocess.TimeoutExpired:
                code = None
            if code is not None:
                self.exit_code = code if code >= 0 else 128 - code
                return RunEvent.CHILD_EXITED
            if self._lost.is_set():
                self.exit_code, self._forward = EXIT_LEASE_LOST, signal.SIGTERM
                return RunEvent.LEASE_LOST
            if self._signum:
                self._forward = signal.Signals(self._signum)
                return self._signalled()

    def _stopping(self) -> RunEvent:
        child = self._child
        assert child is not None
        self._signal_group(self._forward)
        try:
            child.wait(timeout=self._grace_s)
        except subprocess.TimeoutExpired:
            pass
        self._kill_group()
        child.wait()
        return RunEvent.CHILD_EXITED

    def _signal_group(self, number: int) -> None:
        if self._child is None:
            return
        try:
            os.killpg(self._child.pid, number)
        except ProcessLookupError:
            return

    def _kill_group(self) -> None:
        """Kill what is left of the child's group; the child is reaped here."""
        self._signal_group(signal.SIGKILL)
        if self._child is not None:
            self._child.wait()

    def _reap_stray_child(self) -> None:
        """An error path out of the loop never leaves the child running."""
        if self._child is not None and self._child.poll() is None:
            self._kill_group()

    def _releasing(self) -> RunEvent:
        self._beat_stop.set()
        if self._beat is not None:
            self._beat.join(timeout=_DOOR_TIMEOUT_S + 1)
        if self._lease:
            self._release_with_retries()
        return RunEvent.RELEASE_DONE

    def _release_with_retries(self) -> None:
        """Three attempts in five seconds; the engine's TTL frees a lost release."""
        key = uuid.uuid4().hex
        started = self._clock()
        for attempt in range(RELEASE_ATTEMPTS):
            try:
                self._door.call(
                    "capacity.lease.release", _fence(self._lease, self._ttl_ms), key=key
                )
                return
            except DoorRefused as refused:
                if refused.code == "TRANSITION_REFUSED":
                    return  # already released, expired or revoked
            except DoorUnreachable:
                pass
            left = RELEASE_BUDGET_S - (self._clock() - started)
            if left <= 0 or attempt == RELEASE_ATTEMPTS - 1:
                return
            self._wake.wait(min(1.0, left))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="arbora lease run",
        description="Run a command under one build-slot lease: arbora lease run "
        "--pool CELL --priority land-gate|train|lane [--wait-secs N] [--ttl-ms N] "
        "-- ARGV...",
    )
    parser.add_argument("--pool", required=True)
    parser.add_argument(
        "--priority", required=True, choices=("land-gate", "train", "lane")
    )
    parser.add_argument("--wait-secs", type=float, default=DEFAULT_WAIT_S)
    parser.add_argument("--ttl-ms", type=int, default=DEFAULT_TTL_MS)
    parser.add_argument("--url", default="")
    parser.add_argument(
        "--poll-secs", type=float, default=POLL_S, help=argparse.SUPPRESS
    )
    return parser


def _split(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    items = list(argv)
    if "--" not in items:
        return items, []
    cut = items.index("--")
    return items[:cut], items[cut + 1 :]


def main(argv: Sequence[str]) -> int:
    """The command entry: parse, build the door from the configured identity, run."""
    from arbora.core.environment import read_env

    options, child = _split(argv)
    try:
        args = _parser().parse_args(options)
    except SystemExit as exit_:
        return EXIT_USAGE if exit_.code else 0
    if not child or args.ttl_ms < 1:
        print("LEASE_RUN_USAGE: a command after -- is required", file=sys.stderr)
        return EXIT_USAGE
    try:
        door = HttpDoor(
            args.url or read_env("ARBORA_URL", ""), read_env("ARBORA_TOKEN", "")
        )
    except ValueError:
        print("LEASE_DOOR_UNCONFIGURED", file=sys.stderr)
        return EXIT_REFUSED
    try:
        return LeaseRun(
            door,
            pool=args.pool,
            priority=args.priority.replace("-", "_"),
            argv=child,
            wait_secs=args.wait_secs,
            ttl_ms=args.ttl_ms,
            poll_s=args.poll_secs,
        ).run()
    except RunTransitionRefused as refused:
        print(json.dumps({"code": refused.code}), file=sys.stderr)
        return 70


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
