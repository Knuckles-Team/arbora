"""``arbora deploy run``: the deploy as an explicit state machine (ARBORA-DEPLOY-R602.5).

``DeployRun`` has eleven states and fourteen events; the legal edges are the
table :data:`LEGAL`, and any other pair is refused with
``DEPLOY_TRANSITION_ILLEGAL`` naming the state and the event. The state and the
events already applied are persisted in ``deploy-run.json`` in the work
directory, written only by :meth:`DeployRun.apply`; a re-run resumes from the
stored state and a completed step is not repeated. A step holds no logic of its
own: each event is a table row of the commands it runs, which are the
subcommands of ``arbora deploy`` and ``kubectl`` calls on the rendered files.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from arbora.deployment.provision import ConfigError, DeployConfig

STATE_FILE = "deploy-run.json"
TRANSITION_ILLEGAL = "DEPLOY_TRANSITION_ILLEGAL"


class DeployState(Enum):
    PLANNED = "Planned"
    PREFLIGHT_PASSED = "PreflightPassed"
    BACKED_UP = "BackedUp"
    CONFIGURED = "Configured"
    BUILT = "Built"
    APPLIED = "Applied"
    CUT_OVER = "CutOver"
    SERVING = "Serving"
    VERIFIED = "Verified"
    ROLLED_BACK = "RolledBack"
    FAILED = "Failed"


class DeployEvent(Enum):
    PREFLIGHT = "Preflight"
    BACKUP = "Backup"
    CONFIG = "Config"
    BUILD = "Build"
    APPLY = "Apply"
    CUTOVER = "Cutover"
    FIRST_START = "FirstStart"
    SERVE = "Serve"
    ONBOARD = "Onboard"
    ENROL = "Enrol"
    INGEST = "Ingest"
    VERIFY = "Verify"
    ROLLBACK = "Rollback"
    ABORT = "Abort"


TERMINAL = frozenset(
    {DeployState.VERIFIED, DeployState.ROLLED_BACK, DeployState.FAILED}
)
S, E = DeployState, DeployEvent

#: The forward chain in step order. The steps that leave the stage unchanged
#: (FirstStart, Onboard, Enrol, Ingest) are edges from a state to itself.
FORWARD: tuple[tuple[DeployState, DeployEvent, DeployState], ...] = (
    (S.PLANNED, E.PREFLIGHT, S.PREFLIGHT_PASSED),
    (S.PREFLIGHT_PASSED, E.BACKUP, S.BACKED_UP),
    (S.BACKED_UP, E.CONFIG, S.CONFIGURED),
    (S.CONFIGURED, E.BUILD, S.BUILT),
    (S.BUILT, E.APPLY, S.APPLIED),
    (S.APPLIED, E.CUTOVER, S.CUT_OVER),
    (S.CUT_OVER, E.FIRST_START, S.CUT_OVER),
    (S.CUT_OVER, E.SERVE, S.SERVING),
    (S.SERVING, E.ONBOARD, S.SERVING),
    (S.SERVING, E.ENROL, S.SERVING),
    (S.SERVING, E.INGEST, S.SERVING),
    (S.SERVING, E.VERIFY, S.VERIFIED),
)
ROLLBACK_FROM = (S.APPLIED, S.CUT_OVER, S.SERVING)

#: Every legal (state, event) pair and the state it leads to.
LEGAL: dict[tuple[DeployState, DeployEvent], DeployState] = {
    **{(state, event): target for state, event, target in FORWARD},
    **{(state, E.ROLLBACK): S.ROLLED_BACK for state in ROLLBACK_FROM},
    **{(state, E.ABORT): S.FAILED for state in DeployState if state not in TERMINAL},
}
CHAIN = tuple(event for _, event, _ in FORWARD)


class TransitionIllegal(ValueError):
    """An event the state does not accept."""

    def __init__(self, state: DeployState, event: DeployEvent) -> None:
        self.code = TRANSITION_ILLEGAL
        super().__init__(
            f"{TRANSITION_ILLEGAL}: state {state.value} does not accept event {event.value}"
        )


def next_state(state: DeployState, event: DeployEvent) -> DeployState:
    """The state ``event`` leads to from ``state``, or :class:`TransitionIllegal`."""
    try:
        return LEGAL[(state, event)]
    except KeyError:
        raise TransitionIllegal(state, event) from None


# -- the step table ----------------------------------------------------------

Command = list[str]
Runner = Callable[[Command], int]


@dataclass(frozen=True)
class StepContext:
    config: DeployConfig
    config_path: Path
    workdir: Path

    def arbora(self, *words: str) -> Command:
        return ["arbora", "deploy", *words]

    @property
    def manifests(self) -> str:
        return str(self.workdir / "manifests.yaml")

    @property
    def job(self) -> str:
        return str(self.workdir / "first-start-job.yaml")

    def render(self, *flags: str, out: str) -> Command:
        return self.arbora(
            "render",
            "--topology",
            self.config.topology,
            "--contract",
            self.config.contract,
            "--out",
            out,
            *flags,
        )

    def kube(self, *words: str) -> Command:
        return ["kubectl", "-n", self.config.namespace, *words]


#: Each event is the commands it runs, in order; none holds logic of its own.
STEPS: dict[DeployEvent, Callable[[StepContext], list[Command]]] = {
    E.PREFLIGHT: lambda c: [
        c.arbora("normalize-config", "--check", "--config", str(c.config_path)),
        ["kubectl", "version", "--client"],
    ],
    E.BACKUP: lambda c: [
        c.kube("get", "deployment,configmap,service,ingress", "-o", "yaml")
    ],
    E.CONFIG: lambda c: [c.arbora("normalize-config", "--config", str(c.config_path))],
    E.BUILD: lambda c: [c.render(out=c.manifests)],
    E.APPLY: lambda c: [
        ["kubectl", "apply", "-f", c.manifests],
        c.arbora(
            "provision",
            "--apply",
            "--workdir",
            str(c.workdir / "provision"),
            "--config",
            str(c.config_path),
        ),
    ],
    E.CUTOVER: lambda c: [
        c.kube("scale", f"deployment/{c.config.workload}", "--replicas=0")
    ],
    E.FIRST_START: lambda c: [
        c.render("--first-start", out=c.job),
        ["kubectl", "apply", "-f", c.job],
        c.kube(
            "wait",
            "--for=condition=complete",
            f"job/{c.config.workload}-first-start",
            "--timeout=7200s",
        ),
    ],
    E.SERVE: lambda c: [
        c.kube("scale", f"deployment/{c.config.workload}", "--replicas=1"),
        c.kube("rollout", "status", f"deployment/{c.config.workload}"),
    ],
    E.ONBOARD: lambda c: [
        c.arbora("check-in-pod", "--config", str(c.config_path), "verify")
    ],
    E.ENROL: lambda c: [c.arbora("enrol", "--config", str(c.config_path))],
    E.INGEST: lambda c: [
        c.arbora("check-in-pod", "--config", str(c.config_path), "ingest")
    ],
    E.VERIFY: lambda c: [
        c.arbora("probe-sso", "--config", str(c.config_path)),
        c.kube("rollout", "status", f"deployment/{c.config.workload}"),
    ],
    E.ROLLBACK: lambda c: [
        c.render("--rollback", out=str(c.workdir / "rollback.yaml")),
        ["kubectl", "apply", "-f", str(c.workdir / "rollback.yaml")],
    ],
}


def subprocess_runner(command: Command) -> int:
    return subprocess.run(command, check=False).returncode  # noqa: S603  # nosec B603 - argv built from the validated deploy file


# -- the machine -------------------------------------------------------------


@dataclass
class DeployRun:
    """One deploy in a work directory; the only writer of ``deploy-run.json``."""

    workdir: Path
    state: DeployState = DeployState.PLANNED
    completed: list[DeployEvent] = field(default_factory=list)

    @classmethod
    def resume(cls, workdir: Path) -> DeployRun:
        path = workdir / STATE_FILE
        if not path.exists():
            return cls(workdir)
        stored = json.loads(path.read_text())
        return cls(
            workdir,
            DeployState(stored["state"]),
            [DeployEvent(e) for e in stored["completed"]],
        )

    def apply(self, event: DeployEvent) -> DeployState:
        """Take ``event`` from the current state and persist the result."""
        target = next_state(self.state, event)
        self.state = target
        if event not in (E.ABORT, E.ROLLBACK):
            self.completed.append(event)
        self._write()
        return target

    def _write(self) -> None:
        self.workdir.mkdir(parents=True, exist_ok=True)
        document: dict[str, Any] = {
            "state": self.state.value,
            "completed": [e.value for e in self.completed],
        }
        path = self.workdir / STATE_FILE
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2))
        os.replace(temporary, path)

    def done(self, event: DeployEvent) -> bool:
        return event in self.completed


def execute(
    config: DeployConfig,
    config_path: Path,
    workdir: Path,
    runner: Runner,
    *,
    start: DeployEvent | None = None,
    rollback: bool = False,
    steps: dict[DeployEvent, Callable[[StepContext], list[Command]]] = STEPS,
) -> DeployState:
    """Run the deploy from the stored state; a completed step is not repeated."""
    run = DeployRun.resume(workdir)
    context = StepContext(config, config_path, workdir)
    plan: Sequence[DeployEvent] = (E.ROLLBACK,) if rollback else CHAIN
    skipping = start is not None
    for event in plan:
        if run.state in TERMINAL:
            break
        if skipping and event is not start:
            continue
        skipping = False
        if run.done(event):
            continue
        if (run.state, event) not in LEGAL:
            raise TransitionIllegal(run.state, event)
        for command in steps[event](context):
            if runner(command) != 0:
                run.apply(E.ABORT)
                return run.state
        run.apply(event)
    return run.state


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config", required=True, type=Path, help="the deploy.yml file"
    )
    parser.add_argument(
        "--workdir",
        required=True,
        type=Path,
        help="holds deploy-run.json and rendered files",
    )
    parser.add_argument(
        "--from",
        dest="start",
        choices=[e.value for e in CHAIN],
        help="start at this step",
    )
    parser.add_argument(
        "--rollback", action="store_true", help="apply the previous pinned digest"
    )


def main(argv: list[str] | None = None, runner: Runner = subprocess_runner) -> int:
    parser = argparse.ArgumentParser(prog="arbora deploy run", description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(argv)
    try:
        config = DeployConfig.load(args.config)
        final = execute(
            config,
            args.config,
            args.workdir,
            runner,
            start=DeployEvent(args.start) if args.start else None,
            rollback=args.rollback,
        )
    except (ConfigError, TransitionIllegal) as exc:
        print(f"arbora deploy run: {exc}", file=sys.stderr)
        return 2
    print(f"deploy run: {final.value}")
    return 0 if final in (DeployState.VERIFIED, DeployState.ROLLED_BACK) else 1


if __name__ == "__main__":
    raise SystemExit(main())
