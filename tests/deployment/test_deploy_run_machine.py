"""``arbora deploy run`` is an explicit state machine (ARBORA-DEPLOY-R602.5)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbora.deployment import run
from arbora.deployment.provision import DeployConfig
from arbora.deployment.run import DeployEvent as E
from arbora.deployment.run import DeployState as S

CONFIG = {
    "namespace": "apps",
    "workload": "arbora",
    "secret_store": {"mount": "kv", "path": "arbora/engine"},
    "identity": {
        "realm_url": "https://sso.example.test/realms/main",
        "process_client": "arbora",
        "admin_secret": "ops/idp-admin",
        "webui_client": "arbora-ui",
        "webui_redirects": ["https://arbora.example.test/*"],
        "ca_bundle": "/etc/ssl/trust/ca-bundle.pem",
    },
    "tenant": "main",
    "principals": [],
    "config_path": "config.json",
    "registry": "registry.example.test",
    "public_url": "https://arbora.example.test/",
    "topology": "topology.json",
    "contract": "contract.json",
}

# The edges the design names, written out independently of the table under test.
EXPECTED_LEGAL: dict[tuple[S, E], S] = {
    (S.PLANNED, E.PREFLIGHT): S.PREFLIGHT_PASSED,
    (S.PREFLIGHT_PASSED, E.BACKUP): S.BACKED_UP,
    (S.BACKED_UP, E.CONFIG): S.CONFIGURED,
    (S.CONFIGURED, E.BUILD): S.BUILT,
    (S.BUILT, E.APPLY): S.APPLIED,
    (S.APPLIED, E.CUTOVER): S.CUT_OVER,
    (S.CUT_OVER, E.FIRST_START): S.CUT_OVER,
    (S.CUT_OVER, E.SERVE): S.SERVING,
    (S.SERVING, E.ONBOARD): S.SERVING,
    (S.SERVING, E.ENROL): S.SERVING,
    (S.SERVING, E.INGEST): S.SERVING,
    (S.SERVING, E.VERIFY): S.VERIFIED,
    (S.APPLIED, E.ROLLBACK): S.ROLLED_BACK,
    (S.CUT_OVER, E.ROLLBACK): S.ROLLED_BACK,
    (S.SERVING, E.ROLLBACK): S.ROLLED_BACK,
    **{(state, E.ABORT): S.FAILED for state in S if state not in run.TERMINAL},
}


def _config() -> DeployConfig:
    return DeployConfig.from_mapping(CONFIG)


def _at(workdir: Path, state: S, completed: list[E] | None = None) -> run.DeployRun:
    machine = run.DeployRun(workdir, state, completed or [])
    machine._write()
    return machine


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
@pytest.mark.parametrize(
    ("state", "event"),
    sorted(EXPECTED_LEGAL, key=lambda pair: (pair[0].value, pair[1].value)),
)
def test_each_legal_edge_has_a_test(tmp_path: Path, state: S, event: E) -> None:
    machine = _at(tmp_path, state)
    assert machine.apply(event) is EXPECTED_LEGAL[(state, event)]
    stored = json.loads((tmp_path / run.STATE_FILE).read_text())
    assert stored["state"] == EXPECTED_LEGAL[(state, event)].value
    assert run.DeployRun.resume(tmp_path).state is EXPECTED_LEGAL[(state, event)]


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
def test_the_table_is_exactly_the_designed_edges() -> None:
    assert run.LEGAL == EXPECTED_LEGAL
    assert len(S) == 11 and len(E) == 14


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
def test_every_illegal_pair_is_refused(tmp_path: Path) -> None:
    refused = 0
    for state in S:
        for event in E:
            if (state, event) in EXPECTED_LEGAL:
                continue
            machine = _at(tmp_path, state)
            with pytest.raises(
                run.TransitionIllegal, match=run.TRANSITION_ILLEGAL
            ) as raised:
                machine.apply(event)
            assert state.value in str(raised.value) and event.value in str(raised.value)
            assert run.DeployRun.resume(tmp_path).state is state  # nothing was written
            refused += 1
    assert refused == len(S) * len(E) - len(EXPECTED_LEGAL)


class Recorder:
    def __init__(self, fail_on: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: list[str]) -> int:
        self.commands.append(command)
        return 1 if self.fail_on and self.fail_on in command else 0


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
def test_resume_skips_completed_steps(tmp_path: Path) -> None:
    config_path = tmp_path / "deploy.yml"
    first = Recorder(fail_on="rollout")  # the Serve step fails after FirstStart
    final = run.execute(_config(), config_path, tmp_path, first)
    assert final is S.FAILED
    stored = json.loads((tmp_path / run.STATE_FILE).read_text())
    assert stored["state"] == "Failed"
    assert stored["completed"] == [
        "Preflight",
        "Backup",
        "Config",
        "Build",
        "Apply",
        "Cutover",
        "FirstStart",
    ]
    # A re-run from a stored mid-chain state repeats nothing already completed.
    _at(
        tmp_path,
        S.CUT_OVER,
        [E.PREFLIGHT, E.BACKUP, E.CONFIG, E.BUILD, E.APPLY, E.CUTOVER, E.FIRST_START],
    )
    second = Recorder()
    assert run.execute(_config(), config_path, tmp_path, second) is S.VERIFIED
    ran = [" ".join(c) for c in second.commands]
    assert not any(
        "first-start" in c or "--replicas=0" in c or "kubectl apply" in c for c in ran
    )
    assert any("rollout status" in c for c in ran) and any(
        "probe-sso" in c for c in ran
    )
    # A finished run does nothing more.
    third = Recorder()
    assert (
        run.execute(_config(), config_path, tmp_path, third) is S.VERIFIED
        and third.commands == []
    )


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
def test_rollback_applies_the_previous_digest(tmp_path: Path) -> None:
    config_path = tmp_path / "deploy.yml"
    _at(
        tmp_path,
        S.SERVING,
        [
            E.PREFLIGHT,
            E.BACKUP,
            E.CONFIG,
            E.BUILD,
            E.APPLY,
            E.CUTOVER,
            E.FIRST_START,
            E.SERVE,
        ],
    )
    recorder = Recorder()
    assert (
        run.execute(_config(), config_path, tmp_path, recorder, rollback=True)
        is S.ROLLED_BACK
    )
    render = recorder.commands[0]
    assert render[:3] == ["arbora", "deploy", "render"] and "--rollback" in render
    assert recorder.commands[1][:3] == ["kubectl", "apply", "-f"]
    assert recorder.commands[1][3] == render[render.index("--out") + 1]
    # Rollback from a state that has applied nothing is refused.
    other = tmp_path / "fresh"
    with pytest.raises(run.TransitionIllegal):
        run.execute(_config(), config_path, other, Recorder(), rollback=True)


@pytest.mark.spec("ARBORA-DEPLOY-R602.5")
def test_main_reports_the_final_state(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import yaml

    config_path = tmp_path / "deploy.yml"
    config_path.write_text(yaml.safe_dump(CONFIG))
    code = run.main(
        ["--config", str(config_path), "--workdir", str(tmp_path / "w")],
        runner=Recorder(),
    )
    assert code == 0 and "deploy run: Verified" in capsys.readouterr().out
