"""Doctor checks for secrets, identity exposure and authentication (ARBORA-HOST-R038.11)."""

from __future__ import annotations

import logging
from typing import Any

from arbora.core.environment import read_env
from arbora.core.settings import RuntimeConfig
from arbora.deployment.doctor_support import PROBE_FAULTS, _result

logger = logging.getLogger(__name__)


def _check_secrets() -> dict[str, Any]:
    source_status: dict[str, Any] = {
        "state": "not_loaded",
        "present": False,
        "valid": True,
        "referenced_count": 0,
        "matched_count": 0,
        "projected_count": 0,
        "overridden_count": 0,
    }
    try:
        from arbora.core.settings import ArboraConfig
        from arbora.deployment.config_generator import _unresolved_secret_refs

        unresolved = _unresolved_secret_refs(ArboraConfig())
    except PROBE_FAULTS as exc:
        return _result(
            "secrets",
            "fail",
            f"secrets backend not evaluated ({type(exc).__name__})",
            remediation=(
                "repair the private runtime source or configured secret backend"
            ),
            skill="arbora-deployment",
            data={
                "runtime_source": source_status,
                "redacted": True,
            },
        )
    data = {
        "runtime_source": source_status,
        "unresolved_count": len(unresolved),
        "redacted": True,
    }
    if not unresolved:
        return _result(
            "secrets",
            "ok",
            "no unresolved secret references",
            data=data,
        )
    return _result(
        "secrets",
        "fail",
        f"{len(unresolved)} secret reference(s) are unresolved",
        remediation="seed the values in your secrets backend",
        skill="secret-vault-manager",
        data=data,
    )


def _check_secrets_backend() -> dict[str, Any]:
    """Flag a runtime secret reference whose scheme names a backend that the
    *configured* ``SECRETS_BACKEND`` does not match.

    ``SecretsClient.resolve_ref`` resolves every ``vault://``/``secret://``
    reference through whichever backend is active — the scheme itself never
    selects a backend (CONCEPT:MERISTEM-OS.config.secrets-authentication). So a
    ``vault://`` reference silently resolves against the engine-backed
    ``__secrets__`` store instead of the named Vault/OpenBao instance when
    ``SECRETS_BACKEND != "vault"`` — and can even appear "resolved" if a
    same-named key happens to exist in the wrong store, which is why this is a
    *scheme/backend consistency* gate, independent of whether resolution
    happens to succeed (``_check_secrets`` above only catches total failure).
    """
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig
        from arbora.deployment.config_generator import (
            secret_reference_scheme_counts,
        )

        cfg = AgentConfig()
        backend = str(getattr(cfg, "secrets_backend", None) or "vault")
        counts = secret_reference_scheme_counts(cfg)
    except PROBE_FAULTS as exc:
        return _result(
            "secrets_backend",
            "fail",
            f"secret reference scheme scan failed ({type(exc).__name__})",
            remediation="repair AgentConfig construction or the configured secrets backend",
            skill="arbora-deployment",
            data={"redacted": True},
        )

    vault_refs = counts.get("vault", 0)
    engine_refs = counts.get("secret", 0)
    data = {
        "configured_backend": backend,
        "vault_scheme_ref_count": vault_refs,
        "secret_scheme_ref_count": engine_refs,
        "redacted": True,
    }

    if vault_refs and backend != "vault":
        return _result(
            "secrets_backend",
            "fail",
            f"{vault_refs} vault:// reference(s) configured but SECRETS_BACKEND={backend!r}; "
            "they resolve against the engine-backed store instead of Vault/OpenBao",
            remediation=(
                "Set SECRETS_BACKEND=vault so vault:// references resolve against the "
                "actual Vault/OpenBao instance, or replace them with secret:// / env:// "
                "references if the engine-backed store is genuinely intended."
            ),
            skill="secret-vault-manager",
            data=data,
        )

    if engine_refs and backend == "vault":
        return _result(
            "secrets_backend",
            "warn",
            f"{engine_refs} secret:// reference(s) configured while SECRETS_BACKEND='vault' "
            "(best-effort: secret:// does not itself guarantee the value lives in vault)",
            remediation=(
                "Confirm each secret:// reference's value genuinely lives in the "
                "configured vault mount, or rename it vault:// for an explicit, "
                "auditable scheme match."
            ),
            skill="secret-vault-manager",
            data=data,
        )

    return _result(
        "secrets_backend",
        "ok",
        f"secret reference schemes are consistent with SECRETS_BACKEND={backend!r}",
        data=data,
    )


def _check_identity_exposure(config: RuntimeConfig) -> dict[str, Any]:
    """Fail for an exposed none mode or a generated secret in production (R013.3).

    Cases: mode ``none`` bound to a non-loopback host, ``ARBORA_AUTH_NONE_EXPOSE``
    set, or ``GRAPH_SERVICE_AUTH_SECRET`` generated automatically under the
    production profile. Reads the same settings as ``arbora.security.anonymous``.
    """

    from arbora.security.anonymous import loopback

    failures: list[str] = []
    if config.arbora_auth_mode == "none":
        if not loopback(str(config.host)):
            failures.append("identity.exposed_none_mode")
        elif read_env("ARBORA_AUTH_NONE_EXPOSE"):
            failures.append("identity.exposed_none_mode")
    production = str(read_env("ARBORA_PROFILE") or "").lower() == "production"
    if production and read_env("GRAPH_SERVICE_AUTH_SECRET_GENERATED"):
        failures.append("identity.generated_secret")
    if not failures:
        return _result(
            "identity_exposure", "ok", "no exposed none mode or generated secret"
        )
    return _result(
        "identity_exposure",
        "fail",
        "identity exposure: " + ", ".join(failures),
        remediation=(
            "bind none mode to loopback only, clear ARBORA_AUTH_NONE_EXPOSE, and "
            "supply GRAPH_SERVICE_AUTH_SECRET as a vault reference in production"
        ),
        data={"checks": failures},
    )


def _auth_unsecured_result(config: RuntimeConfig) -> dict[str, Any] | None:
    """The finding for a deployment running with no identity (loopback demos)."""
    from arbora.security.anonymous import indicator

    unsecured = indicator(config)
    if not unsecured:
        return None
    return _result(
        "auth",
        unsecured["doctor_check"],
        unsecured["banner"],
        remediation=(
            "use local or external identity mode (docs/identity.md, mode "
            "transition); none mode is for loopback demos only"
        ),
        data={"mode": unsecured["mode"], "principal": unsecured["principal"]},
    )


def _auth_ephemeral_result(config: RuntimeConfig) -> dict[str, Any] | None:
    """The finding for the private ephemeral graph authority of a tiny stdio run."""
    from arbora.security.request_identity import local_process_authority_enabled

    if not local_process_authority_enabled(config):
        return None
    return _result(
        "auth",
        "ok",
        "private ephemeral graph authority is ready for tiny stdio",
        data={
            "mode": "ephemeral_local",
            "network_transport_ready": False,
            "redacted": True,
        },
    )


def _auth_missing_settings(jwks: str) -> list[str]:
    """The verified-authority settings that are absent."""
    audience = str(read_env("AUTH_JWT_AUDIENCE", "") or "").strip()
    policy_version = str(read_env("KG_POLICY_VERSION", "") or "").strip()
    return [
        name
        for name, value in (
            ("AUTH_JWT_JWKS_URI", jwks),
            ("AUTH_JWT_AUDIENCE", audience),
            ("KG_POLICY_VERSION", policy_version),
        )
        if not value
    ]


def _auth_verified_result(jwks: str) -> dict[str, Any]:
    """The finding for a verified graph authority: ready, or incomplete."""
    from arbora.identity.deployment_settings import identity_deployment

    missing = _auth_missing_settings(jwks)
    if not missing:
        # IdP-agnostic: any OIDC issuer's JWKS works. Name it for the report so
        # an operator on Okta isn't told they need Keycloak.
        low = jwks.lower()
        idp = "Okta" if "okta" in low else ("Keycloak" if "keycloak" in low else "OIDC")
        return _result(
            "auth",
            "ok",
            f"verified graph authority configured ({idp}; audience + policy pinned)",
            data={"identity": identity_deployment().report()},
        )
    return _result(
        "auth",
        "fail",
        f"verified graph authority is incomplete ({len(missing)} setting(s) absent)",
        remediation=(
            "configure AUTH_JWT_JWKS_URI, AUTH_JWT_AUDIENCE, and KG_POLICY_VERSION; "
            "served graph operations fail closed until all three are present"
        ),
        skill="keycloak-client-onboarder",
        data={"missing_count": len(missing), "fail_closed": True},
    )


def _check_auth(config: RuntimeConfig) -> dict[str, Any]:
    early = _auth_unsecured_result(config) or _auth_ephemeral_result(config)
    if early is not None:
        return early
    return _auth_verified_result(str(read_env("AUTH_JWT_JWKS_URI", "") or "").strip())


def _check_outbound_auth() -> dict[str, Any]:
    """Validate outbound MCP auth metadata without resolving credential material."""
    try:
        from arbora.security.client_credentials import (
            outbound_auth_configuration_status,
        )

        status = outbound_auth_configuration_status()
    except PROBE_FAULTS as exc:  # never report configured values
        return _result(
            "outbound_auth",
            "fail",
            f"outbound MCP authentication configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Set MCP_CLIENT_AUTH and its canonical AgentConfig fields; keep "
                "credential material behind a runtime secret reference."
            ),
            data={"ready": False, "redacted": True},
        )
    mode = str(status["mode"])
    if mode == "none":
        return _result(
            "outbound_auth",
            "skip",
            "outbound MCP child authentication is disabled",
            data={"mode": mode, "ready": True, "redacted": True},
        )
    if bool(status["ready"]):
        return _result(
            "outbound_auth",
            "ok",
            f"outbound MCP child authentication is configured ({mode})",
            data={"mode": mode, "ready": True, "redacted": True},
        )
    missing = status.get("missing") or []
    invalid = status.get("invalid") or []
    if not isinstance(missing, list):
        missing = []
    if not isinstance(invalid, list):
        invalid = []
    return _result(
        "outbound_auth",
        "fail",
        "outbound MCP child authentication is incomplete",
        remediation=(
            "Configure OIDC_CLIENT_ID, OIDC_CLIENT_SECRET_REF, OIDC_AUDIENCE, "
            "and either OIDC_TOKEN_URL or OIDC_ISSUER in XDG AgentConfig."
        ),
        data={
            "mode": mode,
            "ready": False,
            "missing_count": len(missing),
            "invalid_count": len(invalid),
            "redacted": True,
        },
    )


def _graph_identity_readiness(token_ref: str, oauth2: Any) -> tuple[str, bool]:
    """``(mode, ready)`` for the single configured graph process identity source.

    Only the *reference* is resolved -- no token is minted and no resolved value
    ever leaves this function.
    """
    from scion_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    if token_ref:
        return "token_ref", bool(resolve_runtime_secret_reference(token_ref))
    assert oauth2 is not None
    secret_ref = str(oauth2.get("client_secret") or "")
    client_id = str(oauth2.get("client_id") or "")
    ready = bool(resolve_runtime_secret_reference(secret_ref))
    if client_id.startswith(("vault://", "env://", "secret://")):
        ready = ready and bool(resolve_runtime_secret_reference(client_id))
    return "oauth2_client_credentials", ready


def _check_graph_identity() -> dict[str, Any]:
    """Validate graph process identity without minting or exposing a token."""
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig
        from arbora.security.request_identity import (
            local_process_authority_enabled,
        )

        cfg = AgentConfig()
        token_ref = str(cfg.kg_auth_token_ref or "").strip()
        oauth2 = cfg.kg_identity_oauth2
        if local_process_authority_enabled(cfg):
            return _result(
                "graph_identity",
                "ok",
                "private ephemeral graph process authority is ready",
                data={
                    "mode": "ephemeral_local",
                    "ready": True,
                    "redacted": True,
                },
            )
        if bool(token_ref) == bool(oauth2):
            return _result(
                "graph_identity",
                "fail",
                "graph process identity requires exactly one configured source",
                remediation=(
                    "Configure either KG_AUTH_TOKEN_REF or KG_IDENTITY_OAUTH2 "
                    "in XDG AgentConfig, never raw token material or both sources."
                ),
                data={"ready": False, "redacted": True},
            )
        mode, ready = _graph_identity_readiness(token_ref, oauth2)
        if not ready:
            return _result(
                "graph_identity",
                "fail",
                "graph process identity secret reference is unresolved",
                remediation="Seed the configured identity reference in the secrets backend.",
                data={"mode": mode, "ready": False, "redacted": True},
            )
        return _result(
            "graph_identity",
            "ok",
            f"graph process identity source is ready ({mode})",
            data={"mode": mode, "ready": True, "redacted": True},
        )
    except PROBE_FAULTS as exc:  # doctor remains a redacted boundary
        return _result(
            "graph_identity",
            "fail",
            f"graph process identity configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Validate AgentConfig and resolve its runtime secret references; "
                "no raw token or client secret is accepted."
            ),
            data={"ready": False, "redacted": True},
        )
