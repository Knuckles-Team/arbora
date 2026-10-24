"""Static contract of the shipped Helm chart and single-host Compose project."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "helm" / "arbora"
COMPOSE = ROOT / "deploy" / "compose"
SWARM = ROOT / "deploy" / "swarm" / "stack.yml"


def _templates() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in (CHART / "templates").rglob("*")
        if path.is_file()
    )


def test_chart_is_namespace_safe_and_schema_backed() -> None:
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))
    props = schema["properties"]

    assert props["topology"]["enum"] == ["unified-in-process", "out-of-process-shared"]
    assert props["identity"]["properties"]["mode"]["enum"] == [
        "none",
        "local",
        "external",
    ]
    assert props["secrets"]["properties"]["backend"]["enum"] == ["engine", "vault"]
    assert schema["$defs"]["engine"]["properties"]["placement"]["enum"] == [
        "sidecar",
        "child",
    ]
    rendered_source = _templates()
    for kind in ("Namespace", "ClusterRole", "ClusterRoleBinding", "Secret"):
        assert f"kind: {kind}\n" not in rendered_source


def test_chart_defaults_fail_safe() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

    assert values["arbora"]["image"]["repository"].startswith("registry.invalid/")
    assert values["runtimeSecret"]["optional"] is False
    assert values["topology"] == "unified-in-process"
    assert values["engine"]["placement"] == "sidecar"
    assert values["identity"]["mode"] == "local"
    assert values["identity"]["noneExposeAck"] == ""
    assert values["enableServiceLinks"] is False


def test_chart_guards_none_mode_and_engine_tls() -> None:
    helpers = (CHART / "templates" / "_helpers.tpl").read_text(encoding="utf-8")

    assert "I-UNDERSTAND-ANYONE-WHO-CAN-REACH-THIS-PORT-IS-ADMIN" in helpers
    assert "engine.tls.secretName is required" in helpers
    assert "restartPolicy: Always" in (CHART / "templates" / "arbora.yaml").read_text()


def _runtime_env(path: Path) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition("=")
        pairs[key] = value
    return pairs


def test_compose_is_single_writer_loopback_and_secret_free() -> None:
    compose = yaml.safe_load((COMPOSE / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert list(services) == ["arbora"]
    service = services["arbora"]
    assert "deploy" not in service
    assert service["image"].startswith("${ARBORA_IMAGE:?")
    assert all(port.startswith("127.0.0.1:") for port in service["ports"])
    # The fixed, non-tunable half of the runtime environment ships as a
    # tracked env_file (shared in spirit with deploy/swarm/stack.yml, which
    # cannot use env_file) rather than repeated inline here.
    runtime_env_paths = {
        entry["path"] for entry in service["env_file"] if isinstance(entry, dict)
    }
    assert "./arbora-runtime.env" in runtime_env_paths
    runtime_env = _runtime_env(COMPOSE / "arbora-runtime.env")
    assert runtime_env["GRAPH_SERVICE_PERSIST_DIR"].startswith("/var/lib/")
    environment = json.dumps(service["environment"])
    assert not re.search(r"(SECRET|TOKEN|PASSWORD)\"\s*:\s*\"[^$\"]", environment)
    assert (COMPOSE / ".gitignore").read_text(encoding="utf-8").split() == [
        ".env",
        "arbora.env",
    ]


@pytest.mark.spec("ARBORA-HOST-R001.4")
def test_content_is_found_by_convention_and_packaged() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    # Arbora's own skills/, prompts/ and ontology/ are found by convention
    # (agent_connector_sdk.connector.content_dirs); no provider group remains.
    assert "agent_connector_sdk.skill_providers" not in pyproject
    # Package data is one extension glob per data type across the whole
    # package, so every skill asset and every runtime-loaded file (EG
    # exclusions, the default policy, generated contracts) ships. The release
    # wheel boot gate checks the built wheel itself.
    import tomllib

    patterns = tomllib.loads(pyproject)["tool"]["setuptools"]["package-data"]["arbora"]
    assert all(pattern.startswith("**/*.") for pattern in patterns), patterns
    suffixes = {pattern.removeprefix("**/*") for pattern in patterns}
    package = ROOT / "arbora"
    unpackaged = sorted(
        path.relative_to(package).as_posix()
        for path in package.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".py", ".pyc"}
        and path.suffix not in suffixes
    )
    assert unpackaged == []


def test_swarm_stack_is_single_writer_stop_first_and_pinned() -> None:
    stack = yaml.safe_load(SWARM.read_text(encoding="utf-8"))
    service = stack["services"]["arbora"]
    deploy = service["deploy"]

    assert list(stack["services"]) == ["arbora"]
    assert deploy["replicas"] == 1
    assert deploy["update_config"]["order"] == "stop-first"
    assert deploy["rollback_config"]["order"] == "stop-first"
    assert deploy["placement"]["constraints"] == ["node.labels.arbora.data == true"]
    assert stack["networks"]["arbora"]["driver"] == "overlay"
    assert stack["secrets"]["arbora_service_auth_secret"] == {"external": True}
    assert service["environment"]["GRAPH_SERVICE_ENDPOINTS"] == ""
    assert service["image"].startswith("${ARBORA_IMAGE:?")
