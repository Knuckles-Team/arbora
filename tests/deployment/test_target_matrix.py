"""The deployment target matrix is complete (ARBORA-DEPLOY-R013.4)."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
import yaml

from arbora.api.domain.deployment import genesis_environments as genesis
from arbora.api.domain.deployment.genesis_environments import (
    ORCHESTRATOR_TARGET,
    PRIVILEGES,
    RUNTIMES,
    target_cell_refusal,
)

BUILTIN = Path(genesis.__file__).resolve().parents[4] / "deploy" / "environments"

pytestmark = pytest.mark.spec("ARBORA-DEPLOY-R013.4")

#: One orchestrator per target whose runtime the orchestrator does not name.
FREE_ORCHESTRATOR = {"standalone": "bare-metal", "helm": "kubernetes"}
NAMED_ORCHESTRATORS = {
    ("compose", "docker"): "docker-compose",
    ("compose", "podman"): "podman-compose",
    ("swarm", "docker"): "docker-swarm",
}


def _orchestrator(target: str, runtime: str) -> str | None:
    return FREE_ORCHESTRATOR.get(target) or NAMED_ORCHESTRATORS.get((target, runtime))


def test_every_declared_triple_has_a_refusal_case_and_a_success_case_id() -> None:
    cells = {
        (target, runtime, privilege): _orchestrator(target, runtime)
        for target in sorted(set(ORCHESTRATOR_TARGET.values()))
        for runtime, privilege in itertools.product(RUNTIMES, PRIVILEGES)
    }
    served = {
        cell
        for cell, orchestrator in cells.items()
        if orchestrator is not None
        and target_cell_refusal(orchestrator, cell[1], cell[2]) is None
    }
    # standalone and compose cover every runtime-and-privilege cell, helm and swarm
    # only rootful: no other cell has a success case, and each refuses by name.
    assert {c for c in served if c[0] == "standalone"} == {
        ("standalone", r, p) for r in RUNTIMES for p in PRIVILEGES
    }
    assert {c for c in served if c[0] == "compose"} == {
        ("compose", r, p) for r in RUNTIMES for p in PRIVILEGES
    }
    assert {c for c in served if c[0] in ("helm", "swarm")} == {
        ("helm", "docker", "rootful"),
        ("helm", "podman", "rootful"),
        ("swarm", "docker", "rootful"),
    }
    for (target, runtime, privilege), orchestrator in cells.items():
        if orchestrator is None:
            # No orchestrator runs this target on this runtime: never substituted.
            continue
        refusal = target_cell_refusal(orchestrator, runtime, privilege)
        if (target, runtime, privilege) in served:
            assert refusal is None
        else:
            assert refusal is not None and refusal.startswith("unsupported_target")
    assert target_cell_refusal("docker-compose", "podman", "rootful") is not None


@pytest.mark.parametrize(
    ("orchestrator", "runtime", "privilege"),
    [
        ("kubernetes", "docker", "rootless"),
        ("docker-swarm", "docker", "rootless"),
        ("docker-compose", "podman", "rootful"),
        ("kubernetes", "containerd", "rootful"),
        ("kubernetes", "docker", "privileged"),
        ("nomad", "docker", "rootful"),
    ],
)
def test_a_cell_without_an_adapter_refuses_unsupported_target(
    orchestrator: str, runtime: str, privilege: str
) -> None:
    refusal = target_cell_refusal(orchestrator, runtime, privilege)
    assert refusal is not None and refusal.startswith("unsupported_target:")


def _override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **target: str) -> None:
    ext = tmp_path / "environments"
    ext.mkdir()
    raw = yaml.safe_load((BUILTIN / "dev.yaml").read_text(encoding="utf-8"))
    raw["target"].update(target)
    (ext / "dev.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(genesis, "_extension_dir", lambda: ext)


def test_the_profile_loader_refuses_an_unknown_runtime_and_a_missing_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _override(tmp_path, monkeypatch, runtime="containerd")
    with pytest.raises(genesis.EnvironmentProfileError, match="unsupported_target"):
        genesis.load_environment_profile("dev")
    (tmp_path / "b").mkdir()
    _override(tmp_path / "b", monkeypatch, privilege="rootless")
    with pytest.raises(genesis.EnvironmentProfileError, match="no rootless adapter"):
        genesis.load_environment_profile("dev")


def test_the_shipped_profiles_declare_a_served_cell() -> None:
    for name in ("dev", "test", "prod"):
        profile = genesis.load_environment_profile(name)
        assert (profile.target.runtime, profile.target.privilege) == (
            "docker",
            "rootful",
        )
