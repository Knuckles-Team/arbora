"""The engine admission a served deployment sets agrees with the process identity."""

from __future__ import annotations

import hashlib

import pytest

from arbora.deployment.config_generator import (
    PROCESS_IDENTITY_ROLES,
    generate_engine_admission,
)


def test_engine_admission_binds_one_principal_and_one_request_context() -> None:
    admission = generate_engine_admission(
        principal="arbora-process",
        audience="arbora",
        tenant="acme",
        policy_version="policy-1",
    )
    engine = admission["engine_environment"]
    assert engine["MEDULLA_AUDIENCE"] == "arbora"
    assert engine["MEDULLA_TENANT"] == "acme"
    assert engine["MEDULLA_POLICY_VERSION"] == "policy-1"
    digest = hashlib.sha256(b"arbora-process").hexdigest()
    assert engine["MEDULLA_CONNECTOR_PACK_IMPORTERS"] == (
        f"*=principal:sha256:{digest}"
    )
    assert engine["GRAPH_SERVICE_AUTH_SECRET"].startswith("secret://")
    assert admission["arbora_config"] == {
        "AUTH_JWT_AUDIENCE": "arbora",
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
        allowed_private_hosts=["connector-a.example", "connector-b.example"],
    )
    assert admission["arbora_config"]["MCP_HTTP_ALLOWED_PRIVATE_HOSTS"] == (
        "connector-a.example,connector-b.example"
    )


def test_engine_admission_cli_repeats_the_private_host_option(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from arbora.deployment.cli import main

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
    assert out["arbora_config"]["MCP_HTTP_ALLOWED_PRIVATE_HOSTS"] == "a.svc,b.svc"
