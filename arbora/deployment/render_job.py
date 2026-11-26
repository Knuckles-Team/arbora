"""Manifests derived from the rendered gateway Deployment (ARBORA-DEPLOY-R602.1).

The first-start Job and the isolated pre-release set copy the gateway pod's
image, environment and volumes from the Deployment the topology renderer wrote,
so a change to one cannot leave the other behind. Nothing here names a host,
a domain, a registry or a local path: every such value is already a field of
the topology file the Deployment was rendered from.
"""

from __future__ import annotations

import copy
from typing import Any

import yaml

from arbora.deployment.render import InputError

FIRST_START_SUFFIX = "-first-start"
PRERELEASE_LABEL = "prerelease-test"
_SERVED_PATH = "/mcp"


def _documents(rendered: str) -> list[dict[str, Any]]:
    return [
        document
        for document in yaml.safe_load_all(rendered)
        if isinstance(document, dict)
    ]


def _gateway(documents: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the gateway Deployment and the Service that selects it."""
    for document in documents:
        labels = document.get("metadata", {}).get("labels", {})
        if (
            document.get("kind") == "Deployment"
            and labels.get("graphos_workload") == "gateway"
        ):
            for service in documents:
                if (
                    service.get("kind") == "Service"
                    and service["metadata"]["name"] == document["metadata"]["name"]
                ):
                    return document, service
            raise InputError("the rendered gateway has no Service")
    raise InputError("the rendered manifests hold no gateway Deployment")


def served_url(deployment: dict[str, Any], service: dict[str, Any]) -> str:
    port = service["spec"]["ports"][0]["port"]
    return f"http://{service['metadata']['name']}.{deployment['metadata']['namespace']}.svc.cluster.local:{port}{_SERVED_PATH}"


def _dump(documents: list[dict[str, Any]]) -> str:
    return "---\n".join(
        yaml.safe_dump(document, sort_keys=False, width=120) for document in documents
    )


def first_start_job(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the Job: provision semantic content, then onboard the fleet.

    Run only while the gateway is scaled to zero; the store has one writer.
    """
    deployment, service = _gateway(documents)
    pod = copy.deepcopy(deployment["spec"]["template"]["spec"])
    gateway = pod["containers"][0]
    url = served_url(deployment, service)

    def step(name: str, operation: str) -> dict[str, Any]:
        return {
            "name": name,
            "image": gateway["image"],
            "imagePullPolicy": gateway["imagePullPolicy"],
            "securityContext": gateway.get("securityContext", {}),
            "command": ["arbora-production-ops", operation, "--served-url", url],
            "env": gateway["env"],
            "envFrom": gateway.get("envFrom", []),
            "resources": gateway.get("resources", {}),
            "volumeMounts": gateway.get("volumeMounts", []),
        }

    pod["initContainers"] = [
        *pod.get("initContainers", []),
        step("provision-semantic-content", "provision-semantic-content"),
    ]
    pod["containers"] = [step("onboard-fleet", "onboard-fleet")]
    pod["restartPolicy"] = "Never"
    pod["terminationGracePeriodSeconds"] = 30
    name = deployment["metadata"]["name"] + FIRST_START_SUFFIX
    labels = {"app": name}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": deployment["metadata"]["namespace"],
            "labels": labels,
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 7200,
            "ttlSecondsAfterFinished": 86400,
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


def render_first_start_job(rendered: str) -> str:
    """The first-start Job for rendered manifests, as YAML."""
    return _dump([first_start_job(_documents(rendered))])


def prerelease_manifests(rendered: str, *, image: str, name: str) -> str:
    """An isolated Deployment, first-start Job and Service for a candidate image.

    Every name and label is ``name``, so no production Service selects it; the
    served MCP URL points at the isolated Service and messaging intake is off.
    """
    documents = _documents(rendered)
    deployment, service = _gateway(documents)
    job = first_start_job(documents)
    isolated_url = f"http://{name}.{deployment['metadata']['namespace']}.svc.cluster.local:{service['spec']['ports'][0]['port']}{_SERVED_PATH}"
    override = {
        "ARBORA_SERVED_MCP_URL": isolated_url,
        "MESSAGING_INTAKE_ENABLED": "false",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    }

    def isolate(pod: dict[str, Any]) -> None:
        for container in [*pod.get("initContainers", []), *pod["containers"]]:
            container["image"] = image
            container["imagePullPolicy"] = "IfNotPresent"
            env = [
                entry
                for entry in container.get("env", [])
                if entry["name"] not in override
            ]
            container["env"] = [
                *env,
                *({"name": key, "value": value} for key, value in override.items()),
            ]
            if container.get("command", [None])[0] == "arbora-production-ops":
                container["command"] = [*container["command"][:-1], isolated_url]

    labels = {"app": name, "tier": PRERELEASE_LABEL}
    deployment = copy.deepcopy(deployment)
    deployment["metadata"] = {
        "name": name,
        "namespace": deployment["metadata"]["namespace"],
        "labels": labels,
    }
    deployment["spec"]["replicas"] = 1
    deployment["spec"]["selector"] = {"matchLabels": {"app": name}}
    deployment["spec"]["template"]["metadata"] = {"labels": {"app": name}}
    isolate(deployment["spec"]["template"]["spec"])
    job["metadata"] = {
        "name": name + FIRST_START_SUFFIX,
        "namespace": deployment["metadata"]["namespace"],
        "labels": {"app": name + FIRST_START_SUFFIX},
    }
    job["spec"]["template"]["metadata"] = {"labels": {"app": name + FIRST_START_SUFFIX}}
    isolate(job["spec"]["template"]["spec"])
    isolated_service = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": deployment["metadata"]["namespace"],
            "labels": {"app": name},
        },
        "spec": {
            "selector": {"app": name},
            "ports": copy.deepcopy(service["spec"]["ports"]),
        },
    }
    return _dump([isolated_service, job, deployment])
