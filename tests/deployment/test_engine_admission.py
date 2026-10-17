"""The engine admission a served deployment sets agrees with the process identity."""

from __future__ import annotations

import hashlib

import pytest

from graph_os.deployment.config_generator import (
    PROCESS_IDENTITY_ROLES,
    generate_engine_admission,
)


def test_engine_admission_binds_one_principal_and_one_request_context() -> None:
    admission = generate_engine_admission(
        principal="graph-os-process",
        audience="graph-os",
        tenant="acme",
        policy_version="policy-1",
    )
    engine = admission["engine_environment"]
    assert engine["EPISTEMIC_GRAPH_AUDIENCE"] == "graph-os"
    assert engine["EPISTEMIC_GRAPH_TENANT"] == "acme"
    assert engine["EPISTEMIC_GRAPH_POLICY_VERSION"] == "policy-1"
    assert list(engine["EPISTEMIC_GRAPH_SIGNER_KEYS_SHAPE"]) == ["graph-os-process"]
    digest = hashlib.sha256(b"graph-os-process").hexdigest()
    assert engine["EPISTEMIC_GRAPH_CONNECTOR_PACK_IMPORTERS"] == (
        f"*=principal:sha256:{digest}"
    )
    assert engine["GRAPH_SERVICE_AUTH_SECRET"].startswith("secret://")
    assert admission["graph_os_config"] == {
        "AUTH_JWT_AUDIENCE": "graph-os",
        "KG_POLICY_VERSION": "policy-1",
        "MCP_HTTP_ALLOWED_PRIVATE_HOSTS": "",
    }
    assert admission["process_identity_roles"] == list(PROCESS_IDENTITY_ROLES)
    assert any(
        "provision-semantic-content" in step for step in admission["first_start"]
    )


def test_engine_admission_refuses_a_missing_value() -> None:
    with pytest.raises(ValueError, match="tenant"):
        generate_engine_admission(
            principal="p", audience="a", tenant=" ", policy_version="v"
        )


def test_engine_admission_names_private_connector_hosts() -> None:
    admission = generate_engine_admission(
        principal="p",
        audience="a",
        tenant="t",
        policy_version="v",
        allowed_private_hosts=["github-mcp.mcp.svc", "10.0.0.5"],
    )
    assert admission["graph_os_config"]["MCP_HTTP_ALLOWED_PRIVATE_HOSTS"] == (
        "github-mcp.mcp.svc,10.0.0.5"
    )


def test_engine_admission_cli_repeats_the_private_host_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from graph_os.deployment.cli import main

    assert (
        main(
            [
                "engine-admission",
                "--principal=p",
                "--audience=a",
                "--tenant=t",
                "--policy-version=v",
                "--allowed-private-host=a.svc",
                "--allowed-private-host=b.svc",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["graph_os_config"]["MCP_HTTP_ALLOWED_PRIVATE_HOSTS"] == "a.svc,b.svc"
