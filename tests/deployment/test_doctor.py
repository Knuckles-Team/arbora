"""Tests for the holistic `agent-utilities doctor` health sweep.

The doctor is an aggregator of existing checks; tests assert the contract (report
shape, status precedence, fix routing, defensiveness) with the underlying checks
monkeypatched, plus a live-path through the graph_configure MCP action.
"""

from __future__ import annotations

import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from arbora.deployment import doctor as D


def _ok(name):
    return lambda **kw: D._result(name, "ok", "fine")


def test_module_entrypoint_is_warning_free_and_emits_exact_json() -> None:
    """``python -m`` must not pre-import doctor through the package facade."""
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::RuntimeWarning",
            "-m",
            "arbora.deployment.doctor",
            "--only",
            "python_env",
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    expected = D.run_doctor(["python_env"])
    assert completed.stdout == json.dumps(expected, indent=2, default=str) + "\n"
    assert json.loads(completed.stdout) == expected


def test_deployment_package_preserves_lazy_doctor_exports() -> None:
    """The public facade remains source-compatible after entrypoint hardening."""
    from arbora.deployment import CHECKS, run_doctor, run_preflight

    assert CHECKS is D.CHECKS
    assert run_doctor is D.run_doctor
    assert callable(run_preflight)


def test_deployment_package_lazily_exposes_modules_and_introspection() -> None:
    """Fresh imports preserve facade discovery and direct submodule access."""
    script = """
from concurrent.futures import ThreadPoolExecutor
import json
import sys

import arbora.deployment as deployment

lazy_modules = (
    "arbora.deployment.doctor",
    "arbora.deployment.preflight",
)
assert not any(name in sys.modules for name in lazy_modules)
assert {"CHECKS", "run_doctor", "run_preflight", "doctor", "preflight"} <= set(dir(deployment))

with ThreadPoolExecutor(max_workers=8) as pool:
    doctors = list(pool.map(lambda _: deployment.doctor, range(16)))
assert all(module is doctors[0] for module in doctors)
assert doctors[0].__name__ == "arbora.deployment.doctor"
assert "arbora.deployment.preflight" not in sys.modules

with ThreadPoolExecutor(max_workers=8) as pool:
    preflights = list(pool.map(lambda _: deployment.preflight, range(16)))
assert all(module is preflights[0] for module in preflights)
assert preflights[0].__name__ == "arbora.deployment.preflight"
assert deployment.CHECKS is doctors[0].CHECKS
assert deployment.run_doctor is doctors[0].run_doctor
assert deployment.run_preflight is preflights[0].run_preflight

print(json.dumps({
    "direct_modules": True,
    "introspection": True,
    "lazy_imports": True,
    "thread_safe": True,
}, sort_keys=True))
"""
    completed = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    assert json.loads(completed.stdout) == {
        "direct_modules": True,
        "introspection": True,
        "lazy_imports": True,
        "thread_safe": True,
    }


def test_run_doctor_all_ok(monkeypatch):
    monkeypatch.setattr(D, "CHECKS", {n: _ok(n) for n in ("a", "b", "c")})
    rep = D.run_doctor()
    assert rep["status"] == "healthy"
    assert len(rep["checks"]) == 3
    assert rep["summary"] == "All checks passed."


def test_run_doctor_loads_agent_config_before_checks(monkeypatch):
    observed = {"loaded": False, "checked": False}

    def load_config():
        observed["loaded"] = True

    def check():
        observed["checked"] = observed["loaded"]
        return D._result("config", "ok", "fine")

    monkeypatch.setattr("arbora.core.settings.load_config", load_config)
    monkeypatch.setattr(D, "CHECKS", {"config": check})

    rep = D.run_doctor()

    assert rep["status"] == "healthy"
    assert observed == {"loaded": True, "checked": True}


def test_run_doctor_redacts_configuration_load_failure(monkeypatch):
    def load_config():
        raise ValueError("private configuration detail")

    monkeypatch.setattr("arbora.core.settings.load_config", load_config)
    monkeypatch.setattr(
        D, "CHECKS", {"config": lambda: D._result("config", "ok", "fine")}
    )

    rep = D.run_doctor()

    assert rep["status"] == "unhealthy"
    assert rep["checks"][0]["status"] == "error"
    assert "ValueError" in rep["checks"][0]["detail"]
    assert "private configuration detail" not in json.dumps(rep, sort_keys=True)


def test_run_doctor_worst_status_wins(monkeypatch):
    monkeypatch.setattr(
        D,
        "CHECKS",
        {
            "a": _ok("a"),
            "b": lambda **kw: D._result("b", "warn", "meh"),
            "c": lambda **kw: D._result(
                "c", "fail", "broken", remediation="do x", skill="s"
            ),
        },
    )
    rep = D.run_doctor()
    assert rep["status"] == "unhealthy"  # a fail dominates
    assert rep["counts"]["fail"] == 1 and rep["counts"]["warn"] == 1


def test_run_doctor_skip_is_not_unhealthy(monkeypatch):
    monkeypatch.setattr(
        D, "CHECKS", {"a": _ok("a"), "b": lambda **kw: D._result("b", "skip", "n/a")}
    )
    assert D.run_doctor()["status"] == "healthy"


def test_run_doctor_check_exception_is_contained(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(D, "CHECKS", {"a": _ok("a"), "b": _boom})
    rep = D.run_doctor()
    # The crashing check becomes an 'error' result, doctor still completes.
    b = next(c for c in rep["checks"] if c["name"] == "b")
    assert b["status"] == "error" and "RuntimeError" in b["detail"]
    assert "kaboom" not in b["detail"]
    assert rep["status"] == "unhealthy"


def test_run_doctor_only_filter(monkeypatch):
    monkeypatch.setattr(D, "CHECKS", {n: _ok(n) for n in ("a", "b", "c")})
    rep = D.run_doctor(["b"])
    assert [c["name"] for c in rep["checks"]] == ["b"]


def test_run_doctor_forwards_live_only_to_live_capability_checks(monkeypatch):
    seen = {}

    def live_check(name):
        def check(*, live=False):
            seen[name] = live
            return D._result(name, "ok", "fine")

        return check

    monkeypatch.setattr(
        D,
        "CHECKS",
        {
            "mcp_fleet": live_check("mcp_fleet"),
            "langfuse": live_check("langfuse"),
            "hooks": lambda: D._result("hooks", "ok", "fine"),
        },
    )

    rep = D.run_doctor(live=True)

    assert rep["status"] == "healthy"
    assert seen == {
        "mcp_fleet": True,
        "langfuse": True,
    }


def test_run_doctor_fix_runs_autofix_then_reruns(monkeypatch):
    state = {"fixed": False}

    def flaky(**kw):
        return D._result(
            "hooks",
            "ok" if state["fixed"] else "warn",
            "hooks",
            auto_fixable=True,
        )

    monkeypatch.setattr(D, "CHECKS", {"hooks": flaky})

    def fake_fix(name):
        state["fixed"] = True
        return {"fixed": name, "result": "ok"}

    monkeypatch.setattr(D, "_auto_fix", fake_fix)
    rep = D.run_doctor(fix=True)
    assert rep["fixes"] and rep["fixes"][0]["fixed"] == "hooks"
    # After the fix the re-run flipped the check to ok → overall healthy.
    assert rep["checks"][0]["status"] == "ok"
    assert rep["status"] == "healthy"


def test_individual_checks_never_raise():
    # Every real check must return a dict with the contract keys, never raise,
    # even with nothing deployed.
    for name, fn in D.CHECKS.items():
        res = fn(live=False) if name == "mcp_fleet" else fn()
        assert set(res) >= {"name", "status", "detail"}
        assert res["status"] in ("ok", "warn", "fail", "skip", "error")


def test_permission_governance_doctor_reports_missing_reference_redacted(
    monkeypatch,
):
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(
            permissions_signing_key_ref=None,
            agent_policies_path=None,
            app_profile="dev",
        ),
    )

    result = D._check_permission_governance()

    assert result["status"] == "warn"
    assert result["data"]["redacted"] is True
    assert result["data"]["signing_reference_configured"] is False


def test_permission_governance_doctor_verifies_authority(monkeypatch):
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(
            permissions_signing_key_ref="env://DOCTOR_PERMISSION_KEY",
            agent_policies_path=None,
            app_profile="dev",
        ),
    )
    monkeypatch.setattr(
        "scion_sdk.credentials.runtime_references.resolve_runtime_secret_reference",
        lambda _reference: "doctor-signing-authority-material-32b",
    )

    result = D._check_permission_governance()

    assert result["status"] == "ok"
    assert result["data"]["identity_verified"] is True


@pytest.mark.spec("ARBORA-OPS-R034")
def test_eunomia_doctor_warns_when_narrowing_policy_is_off(monkeypatch):
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(
            eunomia_type="none",
            eunomia_allowed_private_hosts=[],
        ),
    )

    result = D._check_eunomia()

    assert result["status"] == "warn"
    assert result["data"]["mode"] == "none"
    assert result["remediation"]


def test_outbound_auth_doctor_reports_redacted_readiness(monkeypatch):
    monkeypatch.setattr(
        "arbora.mcp_server.backing.client_credentials.outbound_auth_configuration_status",
        lambda: {
            "mode": "oidc-client-credentials",
            "ready": True,
            "missing": (),
            "invalid": (),
            "redacted": True,
        },
    )

    result = D._check_outbound_auth()

    assert result["status"] == "ok"
    assert result["data"] == {
        "mode": "oidc-client-credentials",
        "ready": True,
        "redacted": True,
    }


def test_outbound_auth_doctor_fails_closed_on_missing_audience(monkeypatch):
    # The real ``outbound_auth_configuration_status`` always returns
    # ``missing``/``invalid`` as genuine ``list`` objects (it builds them via
    # ``.extend()``/``.append()``) — ``_check_outbound_auth`` type-checks for
    # exactly that shape and silently treats anything else (e.g. a tuple) as
    # empty, so the mock must match the real contract's type, not just value.
    monkeypatch.setattr(
        "arbora.mcp_server.backing.client_credentials.outbound_auth_configuration_status",
        lambda: {
            "mode": "oidc-client-credentials",
            "ready": False,
            "missing": ["OIDC_AUDIENCE"],
            "invalid": [],
            "redacted": True,
        },
    )

    result = D._check_outbound_auth()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "fail"
    assert result["data"] == {
        "mode": "oidc-client-credentials",
        "ready": False,
        "missing_count": 1,
        "invalid_count": 0,
        "redacted": True,
    }
    assert "env://" not in rendered
    assert "secret://" not in rendered


def test_graph_identity_doctor_validates_token_ref_without_exposing_it(monkeypatch):
    cfg = SimpleNamespace(
        kg_auth_token_ref="secret://graph/private-token",
        kg_identity_oauth2=None,
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    monkeypatch.setattr(
        "scion_sdk.credentials.runtime_references.resolve_runtime_secret_reference",
        lambda _ref: "opaque-runtime-token",
    )

    result = D._check_graph_identity()
    rendered = json.dumps(result, sort_keys=True)
    assert result["status"] == "ok"
    assert result["data"] == {
        "mode": "token_ref",
        "ready": True,
        "redacted": True,
    }
    assert "secret://" not in rendered
    assert "opaque-runtime-token" not in rendered


def test_graph_identity_doctor_rejects_ambiguous_or_missing_source(monkeypatch):
    for cfg in (
        SimpleNamespace(
            kg_auth_token_ref=None,
            kg_identity_oauth2=None,
            deployment_profile="single-node-prod",
            graph_service_endpoints=[],
        ),
        SimpleNamespace(
            kg_auth_token_ref="secret://graph/token",
            kg_identity_oauth2={"client_secret": "secret://graph/client"},
            deployment_profile="tiny",
            graph_service_endpoints=[],
        ),
    ):
        monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda cfg=cfg: cfg)
        result = D._check_graph_identity()
        assert result["status"] == "fail"
        assert result["data"] == {"ready": False, "redacted": True}


def test_graph_identity_doctor_accepts_private_tiny_authority(monkeypatch):
    cfg = SimpleNamespace(
        kg_auth_token_ref=None,
        kg_identity_oauth2=None,
        deployment_profile="tiny",
        graph_service_endpoints=[],
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)

    result = D._check_graph_identity()

    assert result["status"] == "ok"
    assert result["data"] == {
        "mode": "ephemeral_local",
        "ready": True,
        "redacted": True,
    }


def test_transport_security_doctor_resolves_env_profile_without_secret_backend(
    monkeypatch,
):
    monkeypatch.setenv(
        "TEST_TLS_PROFILE_CATALOG",
        json.dumps(
            {
                "profiles": {
                    "private-service": {
                        "system_trust": True,
                        "trust_env": True,
                    }
                }
            }
        ),
    )
    cfg = SimpleNamespace(
        tls_profile="private-service",
        tls_profile_ref=None,
        tls_profiles_ref="env://TEST_TLS_PROFILE_CATALOG",
        tls_ca_bundle_ref=None,
        tls_client_cert_ref=None,
        tls_client_key_ref=None,
        tls_client_key_password_ref=None,
        tls_proxy_url_ref=None,
        tls_system_trust=True,
        tls_trust_env=True,
        engine_tls_profile=None,
        engine_tls_profile_ref=None,
        graph_service_endpoints=[],
        external_graph_connectors=[],
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    with patch(
        "arbora.security.secrets.create_secrets_client",
        side_effect=AssertionError("env references must not open a secret backend"),
    ) as create_client:
        result = D._check_transport_security()

    create_client.assert_not_called()
    assert result["status"] == "ok"
    assert result["data"]["tls"]["verify_enabled"] is True
    assert "external_graph_connectors" not in result["data"]


def test_transport_security_doctor_rejects_invalid_engine_transport(monkeypatch):
    cfg = SimpleNamespace(
        tls_profile=None,
        tls_profile_ref=None,
        tls_profiles_ref=None,
        tls_ca_bundle_ref=None,
        tls_client_cert_ref=None,
        tls_client_key_ref=None,
        tls_client_key_password_ref=None,
        tls_proxy_url_ref=None,
        tls_system_trust=True,
        tls_trust_env=True,
        engine_tls_profile=None,
        engine_tls_profile_ref=None,
        graph_service_endpoints=["http://not-an-engine-transport"],
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)

    result = D._check_transport_security()

    assert result["status"] == "fail"
    assert result["data"]["native_engine_tls"]["ready"] is False


@pytest.mark.parametrize(
    ("client_id", "broker_url", "expected"),
    [
        (None, None, "skip"),
        ("synthetic-client", None, "fail"),
        (None, "https://oauth-broker.example.test", "fail"),
        ("synthetic-client", "https://oauth-broker.example.test", "ok"),
    ],
)
def test_google_workspace_oauth_doctor_is_redacted(
    monkeypatch, client_id, broker_url, expected
):
    cfg = SimpleNamespace(
        google_workspace_oauth_client_id=client_id,
        google_workspace_oauth_broker_url=broker_url,
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)

    result = D._check_google_workspace_oauth()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == expected
    assert "synthetic-client" not in rendered
    assert "oauth-broker.example.test" not in rendered


@pytest.mark.parametrize(
    ("endpoint", "transport", "tls"),
    [
        ("unix:///private/engine.sock", "unix", False),
        ("tcp://engine.invalid:9876", "network", False),
        ("tls://engine.invalid:9876", "network", True),
    ],
)
def test_engine_doctor_reports_only_redacted_public_transport(
    monkeypatch, endpoint, transport, tls
):
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(graph_service_endpoints=[endpoint]),
    )

    result = D._check_engine()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "ok"
    assert result["data"] == {
        "configured_endpoint_count": 1,
        "transport": transport,
        "tls": tls,
        "redacted": True,
    }
    assert endpoint not in rendered


@pytest.mark.parametrize("endpoints", [[], ["tcp://one:1", "tcp://two:2"]])
def test_engine_doctor_requires_one_coordinator(monkeypatch, endpoints):
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(graph_service_endpoints=endpoints),
    )

    result = D._check_engine()

    assert result["status"] == "fail"
    assert result["data"] == {
        "configured_endpoint_count": 0,
        "redacted": True,
    }


def _signed_release_fixture(*, matrix_digest: str) -> dict:
    from scripts.release import check_compatibility

    unsigned = {
        "apiVersion": "arbora.io/v1",
        "kind": "ReleaseManifest",
        "manifestState": "signed-release",
        "releaseId": "release-doctor-fixture",
        "matrixDigest": matrix_digest,
        "configurationDigest": "sha256:" + "1" * 64,
        "protocolSchemas": {},
        "components": {},
        "migrationPlanDigest": "sha256:" + "2" * 64,
        "certificationDigests": {},
        "exactGateEvidence": {},
        "evidence": {},
    }
    return {
        **unsigned,
        "signature": {
            "scheme": "fixture-signature",
            "subjectDigest": check_compatibility.canonical_digest(unsigned),
            "bundleDigest": "sha256:" + "3" * 64,
            "signerIdentityDigest": "sha256:" + "4" * 64,
            "value": "fixture-signature-value",
            "verifierEnv": "RELEASE_VERIFIER_COMMAND",
        },
    }


def _patch_valid_release_verification(monkeypatch) -> MagicMock:
    from scripts.release import check_compatibility

    def verify(manifest, _matrix, **kwargs):
        assert kwargs["verify_signatures"] is True
        assert kwargs["manifest_path"].name == "release.json"
        assert (
            check_compatibility.file_digest(kwargs["matrix_path"])
            == manifest["matrixDigest"]
        )
        check_compatibility._validate_manifest_signature(manifest)
        return {"ok": True, "signaturesVerified": True}

    verifier = MagicMock(side_effect=verify)
    monkeypatch.setattr(check_compatibility, "verify_release_manifest", verifier)
    return verifier


def test_engine_request_context_doctor_reports_current_only_contract():
    result = D._check_engine_request_context()

    assert result["status"] == "ok"
    assert result["data"] == {
        "verified_context_required": True,
        "legacy_protocol_available": False,
        "unauthenticated_transport_available": False,
    }


def test_secret_doctor_fails_closed_when_backend_evaluation_raises(monkeypatch):
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: object())
    monkeypatch.setattr(
        "arbora.deployment.config_generator._unresolved_secret_refs",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    result = D._check_secrets()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "fail"
    assert result["data"]["redacted"] is True
    assert "private detail" not in rendered


def _patch_secrets_backend_scan(monkeypatch, *, backend, counts):
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(secrets_backend=backend),
    )
    monkeypatch.setattr(
        "arbora.deployment.config_generator.secret_reference_scheme_counts",
        lambda _cfg: counts,
    )


def test_secrets_backend_doctor_fails_when_engine_backend_has_vault_refs(
    monkeypatch,
):
    _patch_secrets_backend_scan(
        monkeypatch, backend="engine", counts={"env": 0, "vault": 2, "secret": 0}
    )

    result = D._check_secrets_backend()

    assert result["status"] == "fail"
    assert result["data"] == {
        "configured_backend": "engine",
        "vault_scheme_ref_count": 2,
        "secret_scheme_ref_count": 0,
        "redacted": True,
    }
    assert "SECRETS_BACKEND=vault" in result["remediation"]


def test_secrets_backend_doctor_passes_when_vault_backend_has_vault_refs(
    monkeypatch,
):
    _patch_secrets_backend_scan(
        monkeypatch, backend="vault", counts={"env": 0, "vault": 3, "secret": 0}
    )

    result = D._check_secrets_backend()

    assert result["status"] == "ok"
    assert result["data"]["configured_backend"] == "vault"


def test_secrets_backend_doctor_passes_when_engine_backend_has_only_secret_refs(
    monkeypatch,
):
    _patch_secrets_backend_scan(
        monkeypatch, backend="engine", counts={"env": 1, "vault": 0, "secret": 2}
    )

    result = D._check_secrets_backend()

    assert result["status"] == "ok"


def test_secrets_backend_doctor_warns_when_vault_backend_has_secret_scheme_refs(
    monkeypatch,
):
    _patch_secrets_backend_scan(
        monkeypatch, backend="vault", counts={"env": 0, "vault": 0, "secret": 1}
    )

    result = D._check_secrets_backend()

    assert result["status"] == "warn"
    assert result["data"]["secret_scheme_ref_count"] == 1


def test_secrets_backend_doctor_prefers_fail_when_both_directions_present(
    monkeypatch,
):
    """A vault:// mismatch is the more severe finding; it wins over the warn."""
    _patch_secrets_backend_scan(
        monkeypatch, backend="engine", counts={"env": 0, "vault": 1, "secret": 5}
    )

    result = D._check_secrets_backend()

    assert result["status"] == "fail"


def test_secrets_backend_doctor_ok_with_no_references_at_all(monkeypatch):
    _patch_secrets_backend_scan(
        monkeypatch, backend="engine", counts={"env": 0, "vault": 0, "secret": 0}
    )

    result = D._check_secrets_backend()

    assert result["status"] == "ok"


def test_secrets_backend_doctor_fails_closed_when_scan_raises(monkeypatch):
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: object())
    monkeypatch.setattr(
        "arbora.deployment.config_generator.secret_reference_scheme_counts",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    result = D._check_secrets_backend()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "fail"
    assert result["data"]["redacted"] is True
    assert "private detail" not in rendered


def test_secrets_backend_doctor_never_leaks_reference_paths(monkeypatch):
    """Only counts/backend name may appear — never the reference path/value."""
    _patch_secrets_backend_scan(
        monkeypatch, backend="engine", counts={"env": 0, "vault": 1, "secret": 0}
    )

    result = D._check_secrets_backend()
    rendered = json.dumps(result, sort_keys=True)

    assert "apps/" not in rendered
    assert result["data"]["redacted"] is True


@pytest.mark.parametrize("selection", [[], ["unknown"], ["config", "config"]])
def test_doctor_rejects_empty_unknown_or_duplicate_selection(selection):
    result = D.run_doctor(selection)
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "unhealthy"
    assert result["counts"] == {"error": 1}
    assert result["checks"][0]["name"] == "selection"
    assert result["checks"][0]["data"] == {"redacted": True}
    assert "unknown" not in rendered


def test_mcp_fleet_doctor_returns_only_aggregate_readiness(tmp_path, monkeypatch):
    config_path = tmp_path / "mcp_config.json"
    config_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    private_endpoint = "https://customer-internal.example.test/mcp"
    report = {
        "total": 2,
        "ok": ["public-server"],
        "invalid": {"private-server": private_endpoint},
        "unreachable": {},
        "missing_from_config": ["private-mcp.example.test"],
        "passed": False,
    }
    fake_validator = SimpleNamespace(
        validate=lambda *_args, **_kwargs: report,
        caddy_hosts=lambda: {},
    )
    monkeypatch.setattr(
        "arbora.core.settings.platform_config_file",
        lambda _name: config_path,
    )
    monkeypatch.setattr(D.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(D.importlib, "import_module", lambda _name: fake_validator)

    result = D._check_mcp_fleet(live=False)
    rendered = json.dumps(result, sort_keys=True)

    assert result["data"]["invalid_count"] == 1
    assert result["data"]["missing_route_count"] == 1
    assert result["data"]["redacted"] is True
    assert private_endpoint not in rendered
    assert "private-server" not in rendered


def test_mcp_fleet_secret_doctor_reports_only_aggregate_readiness(monkeypatch):
    direct_material = "direct-runtime-material"
    mapped_material = "mapped-runtime-material"
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(
            mcp_fleet_secret_refs={
                "DIRECT_ALIAS": "secret://fleet/direct",
                "MAPPED_ALIAS": "secret://fleet/mapped",
            }
        ),
    )
    monkeypatch.setattr(
        "arbora.core.settings.setting",
        lambda alias: direct_material if alias == "DIRECT_ALIAS" else None,
    )
    monkeypatch.setattr(
        "scion_sdk.credentials.runtime_references.resolve_runtime_secret_reference",
        lambda _reference: mapped_material,
    )

    result = D._check_mcp_fleet_secrets()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "ok"
    assert result["data"] == {
        "configured_alias_count": 2,
        "direct_alias_count": 1,
        "mapped_alias_count": 1,
        "unresolved_alias_count": 0,
        "redacted": True,
    }
    assert "DIRECT_ALIAS" not in rendered
    assert "MAPPED_ALIAS" not in rendered
    assert direct_material not in rendered
    assert mapped_material not in rendered


def test_mcp_fleet_secret_doctor_fails_closed_and_redacted(monkeypatch):
    private_reference = "secret://fleet/private/value"
    monkeypatch.setattr(
        "arbora.core.settings.ArboraConfig",
        lambda: SimpleNamespace(
            mcp_fleet_secret_refs={"PRIVATE_ALIAS": private_reference}
        ),
    )
    monkeypatch.setattr("arbora.core.settings.setting", lambda _alias: None)
    monkeypatch.setattr(
        "scion_sdk.credentials.runtime_references.resolve_runtime_secret_reference",
        lambda _reference: (_ for _ in ()).throw(RuntimeError("private detail")),
    )

    result = D._check_mcp_fleet_secrets()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "fail"
    assert result["data"]["unresolved_alias_count"] == 1
    assert "PRIVATE_ALIAS" not in rendered
    assert private_reference not in rendered
    assert "private detail" not in rendered


def test_runtime_integrations_doctor_reports_only_redacted_readiness(
    tmp_path, monkeypatch
):
    inventory = tmp_path / "private-inventory.yml"
    inventory.write_text("all: {}", encoding="utf-8")
    private_template = "https://private-gateway.example.test/{server}/mcp"
    private_media_endpoint = "https://private-media.example.test/api"
    cfg = SimpleNamespace(
        infra_inventory_path=str(inventory),
        fleet_mcp_url_template=private_template,
        comfyui_url=private_media_endpoint,
        xtts_url=None,
        openai_tts_url=None,
        whisper_url=None,
        faster_whisper_url=None,
        flux_url=None,
        sd35_url=None,
        hunyuan_url=None,
        svd_url=None,
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)

    result = D._check_runtime_integrations()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "ok"
    assert result["data"]["configured_category_count"] == 3
    assert result["data"]["ready_category_count"] == 3
    assert result["data"]["inventory_format_ready"] is True
    assert result["data"]["media_endpoint_configured_count"] == 1
    assert result["data"]["network_probed"] is False
    assert result["data"]["redacted"] is True
    assert str(inventory) not in rendered
    assert private_template not in rendered
    assert private_media_endpoint not in rendered


def test_runtime_integrations_doctor_warns_without_emitting_missing_path(monkeypatch):
    missing_path = "synthetic-missing-inventory.yml"
    cfg = SimpleNamespace(
        infra_inventory_path=missing_path,
        fleet_mcp_url_template=None,
        comfyui_url=None,
        xtts_url=None,
        openai_tts_url=None,
        whisper_url=None,
        faster_whisper_url=None,
        flux_url=None,
        sd35_url=None,
        hunyuan_url=None,
        svd_url=None,
    )
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)

    result = D._check_runtime_integrations()

    assert result["status"] == "warn"
    assert result["data"]["inventory_file_ready"] is False
    assert result["data"]["inventory_format_ready"] is False
    assert missing_path not in json.dumps(result, sort_keys=True)


def test_runtime_integrations_doctor_redacts_invalid_config_value(monkeypatch):
    private_value = "https://private-runtime.example.test?identity=hidden"

    def invalid_config():
        raise ValueError(private_value)

    monkeypatch.setattr("arbora.core.settings.ArboraConfig", invalid_config)

    result = D._check_runtime_integrations()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "fail"
    assert result["data"] == {"ready": False, "redacted": True}
    assert private_value not in rendered
    assert "identity=hidden" not in rendered


def test_evolution_staging_doctor_reports_readiness_without_path(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    cfg = SimpleNamespace(evolution_staging_root=str(tmp_path))
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)

    result = D._check_evolution_staging()

    assert result["status"] == "ok"
    assert result["data"] == {"configured": True, "usable": True}
    assert str(tmp_path) not in json.dumps(result, sort_keys=True)


def test_observability_doctor_derives_redacted_langfuse_otlp(monkeypatch):
    monkeypatch.setenv("ENABLE_OTEL", "true")
    monkeypatch.setenv("APP_PROFILE", "dev")
    monkeypatch.setenv("LANGFUSE_HOST", "https://telemetry.example.test")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY_REF", "env://TEST_LANGFUSE_PUBLIC")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY_REF", "env://TEST_LANGFUSE_SECRET")
    monkeypatch.setenv("TEST_LANGFUSE_PUBLIC", "synthetic-public")
    monkeypatch.setenv("TEST_LANGFUSE_SECRET", "synthetic-secret")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)

    result = D._check_observability()
    rendered = json.dumps(result, sort_keys=True)

    assert result["status"] == "ok"
    assert result["data"]["endpoint_derived_from_langfuse"] is True
    assert result["data"]["metadata_only"] is True
    assert result["data"]["redacted"] is True
    assert "synthetic-public" not in rendered
    assert "synthetic-secret" not in rendered
    assert "telemetry.example.test" not in rendered


def _langfuse_doctor_config(**overrides):
    values = {
        "langfuse_public_key_ref": "env://TEST_LANGFUSE_PUBLIC",
        "langfuse_secret_key_ref": "env://TEST_LANGFUSE_SECRET",
        "langfuse_tls_profile": None,
        "langfuse_tls_profile_ref": None,
        "langfuse_host": "https://telemetry.example.test",
        "langfuse_persistence_hmac_key_ref": None,
        "langfuse_kg_auto_ingest": False,
        "langfuse_mcp_enabled": False,
        "kg_failure_evolution": True,
        "trace_export_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_langfuse_static_dependencies(monkeypatch, cfg, *, launcher=None):
    monkeypatch.setattr("arbora.core.settings.ArboraConfig", lambda: cfg)
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.langfuse_credentials_configured",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.resolve_langfuse_credentials",
        lambda **_kwargs: ("synthetic-public", "synthetic-secret"),
    )
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.configure_langfuse_trust",
        lambda **_kwargs: SimpleNamespace(valid=True, configured=False),
    )
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.resolve_langfuse_persistence_hmac_key",
        lambda **_kwargs: "synthetic-persistence-key-material-32",
    )
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.langfuse_provider_contract_ready",
        lambda: launcher is not None,
    )


def test_langfuse_doctor_accepts_direct_key_pair_without_refs(monkeypatch):
    """langfuse-agent's own client reads LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY
    directly (no _REF suffix). The doctor must not report the integration as
    unconfigured/incomplete when only that direct pair is set."""
    cfg = _langfuse_doctor_config(
        langfuse_public_key_ref=None,
        langfuse_secret_key_ref=None,
        kg_failure_evolution=False,
        trace_export_enabled=False,
    )
    _patch_langfuse_static_dependencies(monkeypatch, cfg, launcher=None)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-synthetic")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-synthetic")

    result = D._check_langfuse()

    # Without the direct-key fallback this would report "skip" (integration not
    # configured) or "fail" (incomplete pair) even though real credentials are
    # set. No integration is enabled here, so a fully-recognized pair still
    # resolves through to the "ready but disabled" warn -- proving the direct
    # LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY pair was recognized and resolved.
    assert result["status"] == "warn"
    assert result["data"]["credential_pair_configured"] is True
    assert result["data"]["credential_material_ready"] is True


def test_langfuse_launcher_is_required_only_for_mcp(monkeypatch):
    cfg = _langfuse_doctor_config(langfuse_mcp_enabled=False)
    _patch_langfuse_static_dependencies(monkeypatch, cfg, launcher=None)

    result = D._check_langfuse()

    assert result["status"] == "ok"
    assert result["data"]["mcp_launcher_required"] is False
    assert result["data"]["mcp_launcher_available"] is False
    assert result["data"]["live_probed"] is False


def test_langfuse_doctor_rejects_placeholder_material_locally(monkeypatch):
    from arbora.observability.langfuse_trust import LangfuseTrustError

    cfg = _langfuse_doctor_config()
    _patch_langfuse_static_dependencies(monkeypatch, cfg, launcher=None)
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.resolve_langfuse_credentials",
        MagicMock(side_effect=LangfuseTrustError("langfuse_credentials_invalid")),
    )

    result = D._check_langfuse()

    assert result["status"] == "fail"
    assert result["data"]["credential_material_ready"] is False
    assert result["data"]["error_code"] == "langfuse_credentials_invalid"
    assert "placeholder" not in json.dumps(result, sort_keys=True).lower()


def test_langfuse_mcp_fails_closed_without_launcher(monkeypatch):
    cfg = _langfuse_doctor_config(langfuse_mcp_enabled=True)
    _patch_langfuse_static_dependencies(monkeypatch, cfg, launcher=None)

    result = D._check_langfuse()

    assert result["status"] == "fail"
    assert result["data"]["mcp_launcher_required"] is True
    assert result["data"]["error_code"] == "langfuse_mcp_provider_contract_unavailable"


def test_langfuse_persistence_requires_dedicated_key_ref(monkeypatch):
    cfg = _langfuse_doctor_config(langfuse_kg_auto_ingest=True)
    _patch_langfuse_static_dependencies(monkeypatch, cfg, launcher=None)

    result = D._check_langfuse()

    assert result["status"] == "fail"
    assert result["data"]["persistence_enabled"] is True
    assert result["data"]["persistence_key_ref_configured"] is False


def test_langfuse_persistence_ref_must_resolve(monkeypatch):
    cfg = _langfuse_doctor_config(
        langfuse_kg_auto_ingest=True,
        langfuse_persistence_hmac_key_ref="secret://observability/persistence-hmac",
    )
    _patch_langfuse_static_dependencies(monkeypatch, cfg, launcher=None)
    monkeypatch.setattr(
        "arbora.observability.langfuse_trust.resolve_langfuse_persistence_hmac_key",
        MagicMock(side_effect=RuntimeError("private provider detail")),
    )

    result = D._check_langfuse()

    assert result["status"] == "fail"
    assert result["data"]["persistence_key_ready"] is False
    assert "private provider detail" not in json.dumps(result, sort_keys=True)


def _patch_langfuse_mcp_probe(monkeypatch, responses):
    calls = []

    async def call(action, **arguments):
        calls.append((action, arguments))
        payload, is_error = responses[len(calls) - 1]
        if is_error:
            raise RuntimeError("synthetic child error")
        return payload

    monkeypatch.setattr(
        "arbora.deployment.doctor_observability._call_langfuse_child", call
    )
    return calls


def test_langfuse_mcp_live_probe_invokes_posture_and_bounded_trace_list(monkeypatch):
    calls = _patch_langfuse_mcp_probe(
        monkeypatch,
        [
            (
                {
                    "content_capture_enabled": False,
                    "metadata_only": True,
                },
                False,
            ),
            ({"data": [], "meta": {"page": 1}}, False),
        ],
    )

    assert D._probe_langfuse_mcp_visibility(SimpleNamespace()) is True
    assert calls == [
        ("runtime_posture", {}),
        (
            "trace_list",
            {
                "page": 1,
                "limit": 1,
                "fields": "core",
            },
        ),
    ]


@pytest.mark.parametrize(
    "responses",
    [
        [
            (
                {
                    "content_capture_enabled": True,
                    "metadata_only": False,
                },
                False,
            )
        ],
        [
            (
                {
                    "content_capture_enabled": False,
                    "metadata_only": True,
                },
                False,
            ),
            ({"data": []}, True),
        ],
        [
            (
                {
                    "content_capture_enabled": False,
                    "metadata_only": True,
                },
                False,
            ),
            ({"unexpected": []}, False),
        ],
    ],
)
def test_langfuse_mcp_live_probe_fails_closed(monkeypatch, responses):
    _patch_langfuse_mcp_probe(monkeypatch, responses)

    assert D._probe_langfuse_mcp_visibility(SimpleNamespace()) is False
