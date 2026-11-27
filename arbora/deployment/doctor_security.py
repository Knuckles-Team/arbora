"""Doctor checks for the security posture: execution surface, permission governance, transport, egress, eunomia and workspace OAuth (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import ipaddress
import logging
from types import SimpleNamespace
from typing import Any

from arbora.deployment.doctor_support import PROBE_FAULTS, _result

logger = logging.getLogger(__name__)


def _is_loopback_listener(listener: str, aliases: set[str]) -> bool:
    """Whether a bind address is loopback, treating ``aliases`` as loopback names."""
    try:
        return ipaddress.ip_address(listener).is_loopback
    except ValueError:
        return listener in aliases


def _execution_security_hazards(cfg: Any) -> list[str]:
    """The enabled host-execution escape hatches, in report order."""
    hazards: list[str] = []
    if cfg.kg_loop_allow_host_validation:
        hazards.append("develop_loop_host_validation")
    if cfg.messaging_alert_intake_allow_remote:
        hazards.append("remote_alert_intake")
    return hazards


def _execution_security_cors(cfg: Any, hazards: list[str]) -> dict[str, Any] | None:
    """Credentialed CORS must have an explicit, wildcard-free origin allowlist."""
    if cfg.cors_allow_credentials and (
        not cfg.allowed_origins
        or "*" in {value.strip() for value in cfg.allowed_origins.split(",")}
    ):
        return _result(
            "execution_security",
            "fail",
            "credentialed CORS does not have an explicit origin allowlist",
            remediation=(
                "Set ALLOWED_ORIGINS to exact trusted origins or disable "
                "CORS_ALLOW_CREDENTIALS."
            ),
            data={"unsafe_execution_hazards": hazards},
        )
    return None


def _execution_security_host_allowlist(
    cfg: Any, hazards: list[str]
) -> dict[str, Any] | None:
    """A configured Host-header allowlist may never contain a wildcard."""
    if cfg.allowed_hosts and "*" in {
        value.strip() for value in cfg.allowed_hosts.split(",")
    }:
        return _result(
            "execution_security",
            "fail",
            "the REST Host-header allowlist contains a blocked wildcard",
            remediation=("Replace ALLOWED_HOSTS=* with exact authority names."),
            data={"unsafe_execution_hazards": hazards},
        )
    return None


def _execution_security_listener(cfg: Any, hazards: list[str]) -> dict[str, Any] | None:
    """A non-loopback REST listener needs authentication AND a Host allowlist."""
    listener = cfg.host.strip().strip("[]").lower()
    if _is_loopback_listener(listener, {"localhost"}):
        return None
    if not bool(cfg.auth_jwt_jwks_uri):
        return _result(
            "execution_security",
            "fail",
            "a non-loopback REST listener has no authentication boundary",
            remediation=("Configure JWT authentication or bind HOST to loopback."),
            data={"unsafe_execution_hazards": hazards},
        )
    if not cfg.allowed_hosts:
        return _result(
            "execution_security",
            "fail",
            "a non-loopback REST listener has no Host-header allowlist",
            remediation="Set ALLOWED_HOSTS to the exact served authority names.",
            data={"unsafe_execution_hazards": hazards},
        )
    return None


def _execution_security_alert_intake(
    cfg: Any, hazards: list[str]
) -> dict[str, Any] | None:
    """An enabled alert intake needs a token ref, and loopback unless approved."""
    if cfg.messaging_alert_intake_port is None:
        return None
    if not cfg.messaging_alert_intake_token_ref:
        return _result(
            "execution_security",
            "fail",
            "messaging alert intake is enabled without a token reference",
            remediation=(
                "Set MESSAGING_ALERT_INTAKE_TOKEN_REF to a runtime secret-provider "
                "reference or disable MESSAGING_ALERT_INTAKE_PORT."
            ),
            data={"unsafe_execution_hazards": hazards},
        )
    alert_listener = cfg.messaging_alert_intake_host.strip().strip("[]").lower()
    alert_loopback = _is_loopback_listener(alert_listener, {"localhost", "localhost."})
    if not alert_loopback and not cfg.messaging_alert_intake_allow_remote:
        return _result(
            "execution_security",
            "fail",
            "messaging alert intake requests a non-loopback bind without approval",
            remediation=(
                "Bind MESSAGING_ALERT_INTAKE_HOST to loopback or explicitly set "
                "MESSAGING_ALERT_INTAKE_ALLOW_REMOTE=true behind a protected ingress."
            ),
            data={"unsafe_execution_hazards": hazards},
        )
    return None


def _check_execution_security() -> dict[str, Any]:
    """Surface dangerous host-execution escape hatches without executing them."""
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig

        cfg = AgentConfig()
    except PROBE_FAULTS as exc:
        return _result(
            "execution_security",
            "error",
            f"execution security configuration unavailable ({type(exc).__name__})",
        )
    hazards = _execution_security_hazards(cfg)
    # Evaluated in this exact order: a doubly-misconfigured deployment must
    # keep reporting the same first failure it always did.
    for guard in (
        _execution_security_cors,
        _execution_security_host_allowlist,
        _execution_security_listener,
        _execution_security_alert_intake,
    ):
        failure = guard(cfg, hazards)
        if failure is not None:
            return failure
    if hazards:
        return _result(
            "execution_security",
            "warn",
            "dangerous host execution escape hatches are enabled",
            remediation=(
                "Disable unsafe host execution flags and configure governed, "
                "isolated execution backends instead."
            ),
            data={"unsafe_execution_hazards": hazards},
        )
    return _result(
        "execution_security",
        "ok",
        "graph-carried validation and RLM model code cannot use unsafe host fallbacks",
        data={"unsafe_execution_hazards": []},
    )


def _check_permission_governance() -> dict[str, Any]:
    """Validate stable identity authority and policy readiness without disclosure."""

    data = {
        "signing_reference_configured": False,
        "signing_authority_ready": False,
        "custom_policy_configured": False,
        "policy_count": 0,
        "identity_verified": False,
        "redacted": True,
    }
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig
        from arbora.deployment.profile_guard import is_production_profile

        cfg = AgentConfig()
        data["signing_reference_configured"] = bool(cfg.permissions_signing_key_ref)
        data["custom_policy_configured"] = bool(cfg.agent_policies_path)
        if not cfg.permissions_signing_key_ref:
            production = is_production_profile(cfg.app_profile)
            return _result(
                "permission_governance",
                "fail" if production else "warn",
                "stable agent-identity signing authority is not configured",
                remediation=(
                    "Set PERMISSIONS_SIGNING_KEY_REF to an env://, vault://, or "
                    "secret:// runtime reference containing at least 32 bytes."
                ),
                data=data,
            )

        from scion_sdk.credentials.runtime_references import (
            resolve_runtime_secret_reference,
        )

        signing_key = resolve_runtime_secret_reference(cfg.permissions_signing_key_ref)
        if len(signing_key.encode("utf-8")) < 32:
            raise ValueError("signing key is shorter than 32 bytes")
        data.update(signing_authority_ready=True, identity_verified=True)
    except PROBE_FAULTS as exc:  # details and paths stay redacted
        return _result(
            "permission_governance",
            "fail",
            f"permission governance is not ready ({type(exc).__name__})",
            remediation=(
                "Resolve the signing-key reference and repair or remove the "
                "configured policy document; configured policies fail closed."
            ),
            data=data,
        )
    return _result(
        "permission_governance",
        "ok",
        "stable signing authority is ready",
        data=data,
    )


def _resolve_tls_profile_data() -> tuple[Any, Any, Any, dict[str, Any]]:
    """Returns (cfg, resolver, secrets_client, tls_data)."""
    from arbora.core.settings import ArboraConfig as AgentConfig
    from arbora.core.transport import (
        resolve_tls_profile,
        tls_environment_from_config,
    )

    cfg = AgentConfig()
    tls_refs = (
        cfg.tls_profile_ref,
        cfg.tls_profiles_ref,
        cfg.tls_ca_bundle_ref,
        cfg.tls_client_cert_ref,
        cfg.tls_client_key_ref,
        cfg.tls_client_key_password_ref,
        cfg.tls_proxy_url_ref,
        cfg.engine_tls_profile_ref,
    )
    needs_resolver = any(tls_refs)
    resolver = None
    secrets_client = None
    if needs_resolver:
        from scion_sdk.credentials.runtime_references import (
            resolve_runtime_secret_reference,
        )

        # Environment-backed profiles are already materialized in this
        # process and must not open (or autostart) the durable secret
        # backend. The canonical resolver initializes engine/Vault storage
        # only when the selected reference scheme actually requires it.
        resolver = resolve_runtime_secret_reference
        secrets_client = SimpleNamespace(resolve_ref=resolver)
    runtime_env = tls_environment_from_config(cfg)
    trust = resolve_tls_profile(
        "GLOBAL",
        environ=runtime_env,
        resolver=resolver,
    )
    tls_data = {
        "configured": trust.configured,
        "ready": True,
        "source": trust.source,
        "verify_enabled": trust.verify_enabled,
        "system_trust": trust.system_trust,
        "custom_ca": bool(trust.ca_bundle_path or trust.ca_directory),
        "mtls": bool(trust.client_cert_path),
        "proxy": bool(trust.proxy_url),
    }
    trust.cleanup()
    return cfg, resolver, secrets_client, tls_data


def _engine_endpoints(cfg: Any) -> list[str]:
    return [
        str(endpoint).strip()
        for endpoint in (getattr(cfg, "graph_service_endpoints", None) or [])
        if str(endpoint).strip()
    ]


def _engine_endpoints_are_ready(engine_endpoints: list[str]) -> bool:
    from arbora.deployment.production_ops import (
        ProductionOperationError,
        _transport_for_endpoint,
    )

    try:
        for endpoint in engine_endpoints:
            _transport_for_endpoint(endpoint)
    except ProductionOperationError:
        return False
    return True


def _engine_tls_data(cfg: Any, resolver: Any) -> dict[str, bool]:
    from arbora.core.transport import resolve_tls_profile

    trust = resolve_tls_profile(
        "ENGINE",
        profile_name=cfg.engine_tls_profile,
        profile_ref=cfg.engine_tls_profile_ref,
        resolver=resolver,
    )
    data = {
        "verify_enabled": trust.verify_enabled,
        "custom_ca": bool(trust.ca_bundle_path or trust.ca_directory),
        "mtls": bool(trust.client_cert_path),
    }
    trust.cleanup()
    return data


def _resolve_engine_transport_data(cfg: Any, resolver: Any) -> dict[str, Any]:
    engine_endpoints = _engine_endpoints(cfg)
    engine_tls_configured = any(
        endpoint.startswith("tls://") for endpoint in engine_endpoints
    ) or bool(cfg.engine_tls_profile or cfg.engine_tls_profile_ref)
    engine_data: dict[str, Any] = {
        "configured": engine_tls_configured,
        "ready": _engine_endpoints_are_ready(engine_endpoints),
        "endpoint_count": len(engine_endpoints),
        "verify_enabled": True,
        "custom_ca": False,
        "mtls": False,
    }
    if engine_tls_configured:
        engine_data.update(_engine_tls_data(cfg, resolver))
    return engine_data


def _check_transport_security() -> dict[str, Any]:
    """Validate Arbora and engine transport posture without source inspection.

    Connector mapping, catalog, and credential readiness belong to the SDK
    runner and EG receipts. Doctor reports only the transport Arbora itself
    owns and never reconstructs connector contracts from AU internals.
    """
    try:
        cfg, resolver, _secrets_client, tls_data = _resolve_tls_profile_data()
        engine_data = _resolve_engine_transport_data(cfg, resolver)
    except PROBE_FAULTS as exc:  # doctor must remain defensive
        return _result(
            "transport_security",
            "fail",
            f"transport profile validation failed ({type(exc).__name__})",
            remediation=(
                "Configure a named TLS profile with secret refs; do not place "
                "endpoints, credentials, certificate material, or local paths in config."
            ),
            data={"ready": False},
        )

    verification_disabled = not tls_data["verify_enabled"] or not bool(
        engine_data["verify_enabled"]
    )
    if not engine_data["ready"]:
        status = "fail"
        detail = "native engine transport policy is not ready"
    elif verification_disabled:
        status = "warn"
        detail = "one or more runtime transports have TLS verification disabled"
    else:
        status = "ok"
        detail = "runtime trust profile ready"
    return _result(
        "transport_security",
        status,
        detail,
        remediation=(
            None
            if status == "ok"
            else "Repair the configured engine endpoint or enable verified TLS."
        ),
        data={"tls": tls_data, "native_engine_tls": engine_data},
    )


def _check_google_workspace_oauth() -> dict[str, Any]:
    """Validate optional OAuth bootstrap without disclosing tenant configuration."""

    try:
        from arbora.core.settings import ArboraConfig as AgentConfig

        cfg = AgentConfig()
    except PROBE_FAULTS as exc:
        return _result(
            "google_workspace_oauth",
            "error",
            f"OAuth configuration unavailable ({type(exc).__name__})",
        )
    client_ready = bool(cfg.google_workspace_oauth_client_id)
    broker_ready = bool(cfg.google_workspace_oauth_broker_url)
    data = {"client_id_configured": client_ready, "broker_configured": broker_ready}
    if not client_ready and not broker_ready:
        return _result(
            "google_workspace_oauth",
            "skip",
            "Google Workspace OAuth is not configured",
            data=data,
        )
    if not (client_ready and broker_ready):
        return _result(
            "google_workspace_oauth",
            "fail",
            "Google Workspace OAuth configuration is incomplete",
            remediation=(
                "Set both GOOGLE_WORKSPACE_OAUTH_CLIENT_ID and the HTTPS-only "
                "GOOGLE_WORKSPACE_OAUTH_BROKER_URL through AgentConfig/XDG runtime config."
            ),
            data=data,
        )
    return _result(
        "google_workspace_oauth",
        "ok",
        "Google Workspace OAuth runtime connection points are configured",
        data=data,
    )


def _egress_tls_profiles(cfg: Any) -> dict[str, Any]:
    """Resolve the model/embedding/OAuth2-token TLS profiles, in that order."""
    from arbora.core.transport import (
        resolve_tls_profile,
        tls_environment_from_config,
    )

    tls_environment = tls_environment_from_config(cfg)
    return {
        "model": resolve_tls_profile(
            "model",
            profile_name=cfg.model_tls_profile,
            profile_ref=cfg.model_tls_profile_ref,
            environ=tls_environment,
        ),
        "embedding": resolve_tls_profile(
            "embedding",
            profile_name=cfg.embedding_tls_profile,
            profile_ref=cfg.embedding_tls_profile_ref,
            environ=tls_environment,
        ),
        "oauth2_token": resolve_tls_profile(
            "oauth2-token",
            profile_name=cfg.oauth2_token_tls_profile,
            profile_ref=cfg.oauth2_token_tls_profile_ref,
            environ=tls_environment,
        ),
    }


def _egress_tls_data(cfg: Any) -> tuple[dict[str, Any], bool]:
    """(redacted model-transport data, whether any model proxy is configured)."""
    profiles = _egress_tls_profiles(cfg)
    proxy_configured = bool(
        profiles["model"].proxy_url
        or profiles["embedding"].proxy_url
        or profiles["oauth2_token"].proxy_url
    )
    data: dict[str, Any] = {}
    for label, profile in profiles.items():
        data[f"{label}_verify_enabled"] = profile.verify_enabled
        data[f"{label}_custom_ca"] = bool(
            profile.ca_bundle_path or profile.ca_directory
        )
        data[f"{label}_mtls"] = bool(profile.client_cert_path)
    data["oauth2_model_count"] = sum(
        bool(getattr(model, "oauth2", None))
        for model in (*cfg.chat_models, *cfg.embedding_models)
    )
    data["model_proxy_configured"] = proxy_configured
    for profile in profiles.values():
        profile.cleanup()
    return data, proxy_configured


def _check_source_egress() -> dict[str, Any]:
    """Report the shared SSRF/redirect/body boundary without exposing hosts."""
    try:
        from scion_sdk.source_connectors.http_safety import (
            normalize_allowed_hosts,
        )

        from arbora.core.settings import ArboraConfig as AgentConfig

        cfg = AgentConfig()
        private_hosts = normalize_allowed_hosts(cfg.source_http_allowed_private_hosts)
        redirect_hosts = normalize_allowed_hosts(cfg.source_http_allowed_redirect_hosts)
        model_private_hosts = normalize_allowed_hosts(
            cfg.model_http_allowed_private_hosts
        )
        model_tls_data, model_proxy_configured = _egress_tls_data(cfg)
    except PROBE_FAULTS as exc:  # doctor must remain defensive
        return _result(
            "source_egress",
            "fail",
            f"source egress policy is invalid ({type(exc).__name__})",
            remediation=(
                "Use exact hostnames (no URLs or wildcards), bounded response/redirect "
                "limits, and keep browser fetching disabled unless explicitly required."
            ),
            data={"ready": False},
        )

    browser_enabled = bool(cfg.source_http_allow_browser_fetch)
    if model_proxy_configured:
        return _result(
            "source_egress",
            "fail",
            "model transport profile is incompatible with DNS-pinned egress",
            remediation=(
                "Remove the model/embedder/OAuth2 token proxy and use direct TLS with "
                "a runtime CA or mTLS profile so destination DNS and peer identity "
                "can be pinned."
            ),
            data={"ready": False, **model_tls_data},
        )
    return _result(
        "source_egress",
        "warn" if browser_enabled else "ok",
        (
            "bounded source egress is active; browser-backed fetching is explicitly enabled"
            if browser_enabled
            else "bounded source egress is active; private hosts and cross-host redirects are denied by default"
        ),
        remediation=(
            "Disable SOURCE_HTTP_ALLOW_BROWSER_FETCH when rendered-page acquisition is not required."
            if browser_enabled
            else None
        ),
        data={
            "ready": True,
            "private_host_allowlist_count": len(private_hosts),
            "redirect_host_allowlist_count": len(redirect_hosts),
            "model_private_host_allowlist_count": len(model_private_hosts),
            "max_response_bytes": cfg.source_http_max_response_bytes,
            "max_redirects": cfg.source_http_max_redirects,
            "browser_fetch_enabled": browser_enabled,
            **model_tls_data,
        },
    )


def _eunomia_embedded_result(cfg: Any) -> dict[str, Any]:
    """Verdict for ``EUNOMIA_TYPE=embedded``: the policy file must be readable."""
    from pathlib import Path

    policy = str(cfg.eunomia_policy_file or "mcp_policies.json")
    ready = Path(policy).expanduser().is_file()
    return _result(
        "eunomia",
        "ok" if ready else "fail",
        (
            "embedded native MCP policy is configured"
            if ready
            else "embedded MCP policy file is unavailable"
        ),
        remediation=(
            None
            if ready
            else "Set EUNOMIA_POLICY_FILE to a runtime-mounted policy document."
        ),
        data={"mode": "embedded", "ready": ready},
    )


def _eunomia_require_bounded_endpoint(cfg: Any, private_hosts: Any) -> None:
    """Raise unless the remote PDP endpoint is present, allowlisted, and HTTPS."""
    from urllib.parse import urlsplit

    from scion_sdk.source_connectors.http_safety import (
        require_safe_source_url,
    )

    endpoint = str(cfg.eunomia_remote_url or "")
    if not endpoint:
        raise ValueError("remote endpoint is missing")
    host = require_safe_source_url(
        endpoint,
        allowed_private_hosts=private_hosts,
        resolve_dns=False,
    )
    parsed = urlsplit(endpoint)
    insecure_transport = parsed.scheme == "http" and host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }
    if insecure_transport:
        raise ValueError("remote endpoint requires HTTPS")


def _eunomia_tls_data(cfg: Any) -> dict[str, Any]:
    """Redacted TLS posture for the remote PDP transport."""
    from arbora.core.transport import (
        resolve_tls_profile,
        tls_environment_from_config,
    )

    trust = resolve_tls_profile(
        "eunomia",
        profile_name=cfg.eunomia_tls_profile,
        profile_ref=cfg.eunomia_tls_profile_ref,
        environ=tls_environment_from_config(cfg),
    )
    tls_data = {
        "verify_enabled": trust.verify_enabled,
        "custom_ca": bool(trust.ca_bundle_path or trust.ca_directory),
        "mtls": bool(trust.client_cert_path),
        "proxy_configured": bool(trust.proxy_url),
    }
    trust.cleanup()
    return tls_data


def _eunomia_remote_result(cfg: Any, private_hosts: Any) -> dict[str, Any]:
    """Verdict for ``EUNOMIA_TYPE=remote``; raises when the endpoint is unsound."""
    _eunomia_require_bounded_endpoint(cfg, private_hosts)
    tls_data = _eunomia_tls_data(cfg)
    if tls_data["proxy_configured"]:
        raise ValueError("remote policy proxy is incompatible with DNS pinning")
    return _result(
        "eunomia",
        "ok",
        ("remote native MCP policy authorization is bounded and TLS-verified"),
        remediation=None,
        data={
            "mode": "remote",
            "ready": True,
            "private_host_allowlist_count": len(private_hosts),
            "api_key_ref_configured": bool(cfg.eunomia_api_key_ref),
            "timeout_seconds": cfg.eunomia_timeout_seconds,
            "max_response_bytes": cfg.eunomia_max_response_bytes,
            "bulk_check_max": cfg.eunomia_bulk_check_max,
            **tls_data,
        },
    )


def _check_eunomia() -> dict[str, Any]:
    """Validate the native policy-decision-point configuration without I/O."""
    try:
        from scion_sdk.source_connectors.http_safety import (
            normalize_allowed_hosts,
        )

        from arbora.core.settings import ArboraConfig as AgentConfig

        cfg = AgentConfig()
        mode = cfg.eunomia_type
        private_hosts = normalize_allowed_hosts(cfg.eunomia_allowed_private_hosts)
        if mode == "none":
            return _result(
                "eunomia",
                "warn",
                "narrowing Eunomia authorization is off for this deployment",
                remediation=(
                    "Set EUNOMIA_TYPE=embedded (with a runtime policy file) or "
                    "EUNOMIA_TYPE=remote (with a bounded endpoint) so discover, "
                    "load, and call all narrow through Eunomia."
                ),
                data={"mode": "none", "ready": True},
            )
        if mode == "embedded":
            return _eunomia_embedded_result(cfg)
        return _eunomia_remote_result(cfg, private_hosts)
    except PROBE_FAULTS as exc:  # doctor is a defensive boundary
        return _result(
            "eunomia",
            "fail",
            f"native MCP policy configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Configure EUNOMIA_TYPE plus either a runtime policy file or a "
                "bounded HTTPS endpoint, exact private-host allowlist, secret ref, "
                "and EUNOMIA TLS profile in AgentConfig/XDG."
            ),
            data={"ready": False, "redacted": True},
        )
