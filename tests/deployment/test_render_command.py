"""``arbora deploy render``: one topology file to manifests (ARBORA-DEPLOY-R602.1)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml

from arbora.console import main as console_main
from arbora.deployment import cli, render, render_job

FIXTURES = Path(__file__).parent / "fixtures" / "render"
_DOMAIN = ".svc." + "cluster.local"
TOPOLOGY = Path()
CONTRACT = Path()


@pytest.fixture(autouse=True)
def _inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write the topology and contract with the cluster domain and a fresh digest."""
    text = (FIXTURES / "contract.json").read_text().replace("@@SVC@@", _DOMAIN)
    contract = json.loads(text)
    payload = {key: value for key, value in contract.items() if key != "digest"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    contract["digest"] = digest
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "contract.json").write_text(json.dumps(contract))
    topology = (FIXTURES / "topology.json").read_text()
    topology = topology.replace("@@SVC@@", _DOMAIN).replace("@@DIGEST@@", digest)
    (inputs / "topology.json").write_text(topology)
    monkeypatch.setitem(globals(), "TOPOLOGY", inputs / "topology.json")
    monkeypatch.setitem(globals(), "CONTRACT", inputs / "contract.json")


def _render(*extra: str, out: Path) -> int:
    return cli.main(
        [
            "render",
            "--topology",
            str(TOPOLOGY),
            "--contract",
            str(CONTRACT),
            "--out",
            str(out),
            *extra,
        ]
    )


@pytest.mark.spec("ARBORA-DEPLOY-R602.1")
def test_same_topology_renders_byte_identical_manifests(tmp_path: Path) -> None:
    first, second, rollback = (
        tmp_path / "a.yaml",
        tmp_path / "b.yaml",
        tmp_path / "r.yaml",
    )
    assert _render(out=first) == 0
    assert _render(out=second) == 0
    assert first.read_bytes() == second.read_bytes()
    assert _render("--rollback", out=rollback) == 0
    documents = list(yaml.safe_load_all(first.read_text()))
    assert {d["kind"] for d in documents} >= {
        "Deployment",
        "Service",
        "Ingress",
        "HorizontalPodAutoscaler",
    }
    # The trust-store mount is the directory of the topology's ca_bundle_path.
    mounts = [
        m["mountPath"]
        for d in documents
        if d["kind"] == "Deployment"
        for c in d["spec"]["template"]["spec"]["containers"]
        for m in c.get("volumeMounts", [])
        if m["name"] == "ca-bundle"
    ]
    assert mounts and set(mounts) == {"/etc/ssl/trust"}


@pytest.mark.spec("ARBORA-DEPLOY-R602.1")
def test_invalid_topology_exits_two_with_the_field_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = json.loads(TOPOLOGY.read_text())
    document["engine"]["ca_bundle_path"] = "relative/ca-bundle.pem"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(document))
    code = cli.main(["render", "--topology", str(bad), "--contract", str(CONTRACT)])
    assert code == 2
    assert "engine.ca_bundle_path" in capsys.readouterr().err
    document = json.loads(TOPOLOGY.read_text())
    document["workloads"]["gateway"]["image"] = "registry.example.test/arbora:latest"
    bad.write_text(json.dumps(document))
    assert (
        cli.main(["render", "--topology", str(bad), "--contract", str(CONTRACT)]) == 2
    )
    assert "workloads.gateway" in capsys.readouterr().err


@pytest.mark.spec("ARBORA-DEPLOY-R602.1")
def test_module_holds_no_host_literal() -> None:
    forbidden = re.compile(
        r"homelab|\.arpa\b|/home/|/var/|services/[a-z-]+/|registry\.[a-z]",
        re.IGNORECASE,
    )
    for module in (render, render_job):
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert forbidden.search(source) is None, module.__name__
    # The mount path follows the field, not a literal.
    document = json.loads(TOPOLOGY.read_text())
    document["engine"]["ca_bundle_path"] = "/srv/pki/ca-bundle.pem"
    topology = render.validate(
        document, engine_identity_contract=json.loads(CONTRACT.read_text())
    )
    text = render._render(topology)
    assert 'mountPath: "/srv/pki"' in text and "/etc/ssl/trust" not in text


@pytest.mark.spec("ARBORA-DEPLOY-R602.1")
def test_first_start_job_and_prerelease_set_derive_from_the_gateway(
    tmp_path: Path,
) -> None:
    rendered = render.render_manifests(TOPOLOGY, CONTRACT)
    job = yaml.safe_load(render_job.render_first_start_job(rendered))
    gateway = next(
        d
        for d in yaml.safe_load_all(rendered)
        if d["kind"] == "Deployment"
        and d["metadata"]["labels"]["graphos_workload"] == "gateway"
    )
    pod = job["spec"]["template"]["spec"]
    assert job["kind"] == "Job" and pod["restartPolicy"] == "Never"
    assert (
        pod["containers"][0]["image"]
        == gateway["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    assert [c["name"] for c in pod["initContainers"]][
        -1
    ] == "provision-semantic-content"
    image = "registry.example.test/arbora@sha256:" + "a" * 64
    out = tmp_path / "pre.yaml"
    assert (
        _render("--prerelease-image", image, "--prerelease-name", "candidate", out=out)
        == 0
    )
    kinds = {d["kind"]: d for d in yaml.safe_load_all(out.read_text())}
    assert set(kinds) == {"Service", "Job", "Deployment"}
    assert kinds["Deployment"]["metadata"]["name"] == "candidate"
    env = {
        e["name"]: e.get("value")
        for e in kinds["Deployment"]["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert (
        env["MESSAGING_INTAKE_ENABLED"] == "false"
        and "candidate" in env["ARBORA_SERVED_MCP_URL"]
    )


@pytest.mark.spec("ARBORA-DEPLOY-R602.1")
def test_console_routes_deploy_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out = tmp_path / "console.yaml"
    monkeypatch.setattr(
        "sys.argv",
        [
            "arbora",
            "deploy",
            "render",
            "--topology",
            str(TOPOLOGY),
            "--contract",
            str(CONTRACT),
            "--out",
            str(out),
        ],
    )
    with pytest.raises(SystemExit) as stop:
        console_main()
    assert stop.value.code == 0 and out.stat().st_size > 0
