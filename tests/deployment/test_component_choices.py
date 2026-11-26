"""One explicit deploy or reuse choice per component (ARBORA-DEPLOY-R013.2)."""

from __future__ import annotations

import json
import random
from pathlib import Path

import pytest

from arbora.deployment import cli
from arbora.deployment.component_choices import (
    ChoiceRefused,
    ask_once_per_component,
    build_inventory,
)

DECLARED = ["gateway", "engine", "webui", "connector-a"]


def _answers(actions: dict[str, str]) -> list[dict[str, object]]:
    return [
        {
            "component_id": component,
            "action": action,
            "endpoint_ref": f"endpoint://{component}" if action == "reuse" else None,
            "profile": "small",
            "confirmed": True,
        }
        for component, action in actions.items()
    ]


@pytest.mark.spec("ARBORA-DEPLOY-R013.2")
def test_all_new_all_reused_and_mixed_inventories_match_the_presented_choices(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rng = random.Random(4110)
    cases = [
        dict.fromkeys(DECLARED, "deploy"),
        dict.fromkeys(DECLARED, "reuse"),
        {c: rng.choice(["deploy", "reuse"]) for c in DECLARED}
        | {"gateway": "deploy", "engine": "reuse"},
    ]
    digests = set()
    for case in cases:
        inventory = build_inventory(DECLARED, _answers(case))
        assert {c.component_id: c.action for c in inventory.choices} == case
        assert set(inventory.deploy_components) == {
            c for c, a in case.items() if a == "deploy"
        }
        assert set(inventory.reuse_components) == {
            c for c, a in case.items() if a == "reuse"
        }
        # Only deploy components go to the engine placement question.
        assert set(inventory.placement_components()) == set(inventory.deploy_components)
        assert (
            "tcp" not in inventory.choices_digest
            and inventory.choices_digest.startswith("sha256:")
        )
        digests.add(inventory.choices_digest)
    assert len(digests) == 3
    # The digest is stable under answer order, and changes with a choice.
    shuffled = _answers(cases[2])[::-1]
    assert (
        build_inventory(DECLARED, shuffled).choices_digest
        == build_inventory(DECLARED, _answers(cases[2])).choices_digest
    )
    # The command asks once per component, or reads a file, and prints the inventory.
    asked: list[str] = []

    def ask(component: str) -> dict[str, str] | None:
        asked.append(component)
        return {"action": "deploy"}

    inventory = ask_once_per_component(DECLARED, ask, profile="small")
    assert asked == DECLARED and inventory.reuse_components == ()
    root = tmp_path / "manifests"
    root.mkdir()
    image = "registry.example.test/x@sha256:" + "a" * 64
    (root / "demo.yaml").write_text(
        "schema_version: 1\nname: demo\ncomponents:\n"
        + "".join(
            f"  - id: {c}\n    kind: connector\n    image: {image}\n    revision: r1\n"
            "    identity_mode: none\n    capacity: {cpu_millis: 100, memory_bytes: 1024, replicas: 1}\n"
            for c in DECLARED
        )
    )
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(_answers(cases[2])))
    code = cli.main(["onboard", "demo", "--root", str(root), "--answers", str(answers)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and printed["choices_digest"].startswith("sha256:")
    assert set(printed["placement_components"]) | set(
        printed["reuse_components"]
    ) == set(DECLARED)


@pytest.mark.spec("ARBORA-DEPLOY-R013.2")
def test_missing_conflicting_and_undeclared_choices_refuse(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    complete = _answers(dict.fromkeys(DECLARED, "deploy"))
    with pytest.raises(ChoiceRefused) as missing:
        build_inventory(DECLARED, complete[:-1])
    assert missing.value.code == "CHOICE_MISSING" and "connector-a" in str(
        missing.value
    )
    unconfirmed = [{**complete[0], "confirmed": False}, *complete[1:]]
    with pytest.raises(ChoiceRefused) as pending:
        build_inventory(DECLARED, unconfirmed)
    assert pending.value.code == "CHOICE_MISSING"
    with pytest.raises(ChoiceRefused) as undeclared:
        build_inventory(DECLARED, [*complete, {**complete[0], "component_id": "ghost"}])
    assert undeclared.value.code == "COMPONENT_UNDECLARED"
    no_endpoint = _answers({**dict.fromkeys(DECLARED, "deploy"), "engine": "reuse"})
    for row in no_endpoint:
        if row["component_id"] == "engine":
            row["endpoint_ref"] = None
    with pytest.raises(ChoiceRefused) as bare:
        build_inventory(DECLARED, no_endpoint)
    assert bare.value.code == "ENDPOINT_CONFLICT"
    shared = _answers(
        {**dict.fromkeys(DECLARED, "deploy"), "engine": "reuse", "webui": "reuse"}
    )
    for row in shared:
        if row["action"] == "reuse":
            row["endpoint_ref"] = "endpoint://one"
    with pytest.raises(ChoiceRefused) as clash:
        build_inventory(DECLARED, shared)
    assert clash.value.code == "ENDPOINT_CONFLICT"
    secret = _answers({**dict.fromkeys(DECLARED, "deploy"), "engine": "reuse"})
    for row in secret:
        if row["action"] == "reuse":
            row["endpoint_ref"] = "tcp://" + "user" + ":" + "pw" + "@host:1"
    with pytest.raises(ChoiceRefused) as leaked:
        build_inventory(DECLARED, secret)
    assert leaked.value.code == "ENDPOINT_CONFLICT"
    # The command refuses with the same code and prints no inventory.
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps(complete[:-1]))
    root = tmp_path / "m"
    root.mkdir()
    image = "registry.example.test/x@sha256:" + "a" * 64
    (root / "demo.yaml").write_text(
        "schema_version: 1\nname: demo\ncomponents:\n"
        + "".join(
            f"  - id: {c}\n    kind: connector\n    image: {image}\n    revision: r1\n"
            "    identity_mode: none\n    capacity: {cpu_millis: 100, memory_bytes: 1024, replicas: 1}\n"
            for c in DECLARED
        )
    )
    assert (
        cli.main(["onboard", "demo", "--root", str(root), "--answers", str(answers)])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["code"] == "CHOICE_MISSING"
