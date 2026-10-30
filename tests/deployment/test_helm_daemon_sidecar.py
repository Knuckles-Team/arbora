"""ARBORA-DEPLOY-R016: the serving container is the one host-role process.

The separate ``daemon`` container is retired (ARBORA-HOST-R004 removed the
``arbora-daemon`` console script, whose entry point was never served). In
every placement the pod runs no ``daemon`` container; the in-pod serving
container holds the host role whenever the engine is in the same pod.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.deployment.test_helm_extensions import _render


def _deployment(documents: list[dict[str, Any]]) -> dict[str, Any]:
    return next(
        document
        for document in documents
        if document["kind"] == "Deployment"
        and document["metadata"]["labels"].get("app.kubernetes.io/component")
        == "arbora"
    )


def _configmap_role(documents: list[dict[str, Any]]) -> str:
    configmap = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap"
        and document["metadata"]["name"].endswith("-arbora")
    )
    return configmap["data"]["KG_DAEMON_ROLE"]


def _container(pod_spec: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next(
        (c for c in pod_spec["containers"] if c["name"] == name),
        None,
    )


@pytest.mark.spec("ARBORA-DEPLOY-R016")
@pytest.mark.parametrize("mode", ["sidecar", "child", "shared"])
def test_no_daemon_container_and_serving_container_holds_host_role(
    tmp_path: Path, mode: str
) -> None:
    values: dict[str, Any] = {"arbora": {"image": {"digest": "sha256:" + "d" * 64}}}
    if mode == "shared":
        values["topology"] = "out-of-process-shared"
        values["engine"] = {"tls": {"secretName": "fixture-engine-tls"}}
    elif mode == "child":
        values["engine"] = {"placement": "child"}
    documents = _render(tmp_path, values)
    pod_spec = _deployment(documents)["spec"]["template"]["spec"]
    assert _container(pod_spec, "daemon") is None
    assert all("arbora-daemon" not in (c.get("command") or []) for c in pod_spec["containers"])
    assert _configmap_role(documents) == ("client" if mode == "shared" else "host")
