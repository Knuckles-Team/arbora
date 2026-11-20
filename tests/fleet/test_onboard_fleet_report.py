"""The onboard-fleet exit rule and the sandbox fleet profile."""

from dataclasses import dataclass
from pathlib import Path

import pytest

from arbora.deployment import production_ops
from arbora.deployment.sandbox_fleet import (
    PROFILE_PATH,
    SandboxFleetProfile,
    SandboxProfileError,
    load_sandbox_profile,
    onboarding_ok,
    select_endpoints,
)

PROFILE = SandboxFleetProfile(connectors=frozenset({"alpha", "beta"}))


@pytest.mark.spec("ARBORA-FLEET-R085.1")
def test_exit_status_follows_the_declared_sandbox_set():
    # A declared connector failing fails the sandbox run.
    assert onboarding_ok({"alpha": "Timeout: x"}, PROFILE) is False
    # An undeclared connector failing stays in the report and leaves the run ok.
    assert onboarding_ok({"gamma": "Timeout: x"}, PROFILE) is True
    # Shared infrastructure failures always fail, in every mode.
    assert onboarding_ok({"registry": "Error: x"}, PROFILE) is False
    assert onboarding_ok({"child-auth": "Error: x"}, PROFILE) is False
    # One failure never hides the others: a mixed set still fails on the declared one.
    assert onboarding_ok({"gamma": "e", "beta": "e"}, PROFILE) is False
    assert onboarding_ok({}, PROFILE) is True
    # Production keeps its current rule: any failure fails.
    assert onboarding_ok({"gamma": "Timeout: x"}, None) is False
    assert onboarding_ok({}, None) is True
    # The exit code maps from the report's own ok flag.
    assert production_ops._exit_status({"ok": False}) == 1
    assert production_ops._exit_status({"ok": True}) == 0


@dataclass
class _Endpoint:
    name: str


@pytest.mark.spec("ARBORA-FLEET-R085.1")
def test_the_sandbox_profile_is_a_closed_declared_set_selected_only_on_request():
    shipped = load_sandbox_profile()
    assert shipped.connectors
    assert PROFILE_PATH.is_file()
    endpoints = [_Endpoint("alpha"), _Endpoint("gamma"), _Endpoint("beta")]
    assert [e.name for e in select_endpoints(endpoints, PROFILE)] == ["alpha", "beta"]
    # The flag is opt-in: production onboarding never reads the profile.
    args = production_ops._parser().parse_args(["onboard-fleet"])
    assert args.sandbox is False
    args = production_ops._parser().parse_args(["onboard-fleet", "--sandbox"])
    assert args.sandbox is True


@pytest.mark.spec("ARBORA-FLEET-R085.1")
@pytest.mark.parametrize(
    "text",
    [
        "not json",
        '{"version": 2, "connectors": ["a"]}',
        '{"version": 1, "connectors": []}',
        '{"version": 1, "connectors": ["a", "a"]}',
        '{"version": 1, "connectors": [1]}',
        "[]",
    ],
)
def test_a_malformed_profile_refuses_and_never_defaults(tmp_path: Path, text: str):
    bad = tmp_path / "profile.json"
    bad.write_text(text, encoding="utf-8")
    with pytest.raises(SandboxProfileError):
        load_sandbox_profile(bad)
    with pytest.raises(SandboxProfileError):
        load_sandbox_profile(tmp_path / "missing.json")
