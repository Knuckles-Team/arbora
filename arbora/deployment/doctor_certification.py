"""Skill and production-certification readiness checks."""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from typing import Any

from arbora.api.errors import not_served

from .doctor_support import _result


def _validate_skill_certification_regular_inputs(
    path_values: tuple[Any, ...],
) -> tuple[Any, bytes, bytes]:
    """Read the four bounded certification inputs; raise if any is unusable.

    Returns ``(configuration_path, configuration, profile)`` -- the reads for the
    release specification and the promotion evidence are performed for their
    validation side effect only.
    """
    from pathlib import Path

    # The host doctor reports certification readiness; the bounded asset
    # readers live in arbora.deployment.certification_assets.
    from arbora.deployment.certification_assets import _read_regular

    if any(value is None for value in path_values):
        raise RuntimeError("skill_certification_path_missing")
    configuration_path = Path(str(path_values[0]))
    configuration = _read_regular(
        configuration_path,
        limit=4 * 1024 * 1024,
        code="runtime_configuration_invalid",
    )
    profile = _read_regular(
        Path(str(path_values[1])),
        limit=4 * 1024 * 1024,
        code="runtime_profile_invalid",
    )
    _read_regular(
        Path(str(path_values[2])),
        limit=4 * 1024 * 1024,
        code="release_specification_invalid",
    )
    _read_regular(
        Path(str(path_values[3])),
        limit=8 * 1024 * 1024,
        code="promotion_evidence_invalid",
    )
    return configuration_path, configuration, profile


def _validate_skill_certification_profile(
    configuration_path: Any, configuration: bytes, profile: bytes
) -> None:
    """Bind the runtime profile to the *active* configuration; raise otherwise."""
    from arbora.core.settings import config_dir
    from arbora.deployment.certification_assets import (
        _configuration_proof,
        _identity_authority_configuration,
        _json_without_duplicates,
        _validate_profile,
    )

    if not configuration_path.samefile(config_dir() / "config.json"):
        raise RuntimeError("runtime_configuration_not_active")
    proof = _configuration_proof(configuration)
    identity_authority = _identity_authority_configuration(
        _json_without_duplicates(configuration, code="runtime_configuration_invalid")
    )
    _validate_profile(
        profile,
        configuration_digest=("sha256:" + hashlib.sha256(configuration).hexdigest()),
        model_registry_digest=str(proof["digest"]),
        identity_authority=identity_authority,
    )


def _validate_skill_certification_commands(
    cfg: Any, command_values: tuple[Any, ...]
) -> None:
    """The Arbora endpoint must be the active one and the argv arrays sound."""

    raise not_served("skills.runtime_validation", "arbora/skills", owner="arbora")


def _validate_skill_certification_material(
    cfg: Any, path_values: tuple[Any, ...], command_values: tuple[Any, ...]
) -> None:
    """Raise unless every exact skill-certification input is present and bound.

    The order matters and is the pre-split order: bounded regular reads first,
    then the active-configuration/profile binding, then the command boundaries.
    """
    configuration_path, configuration, profile = (
        _validate_skill_certification_regular_inputs(path_values)
    )
    _validate_skill_certification_profile(configuration_path, configuration, profile)
    _validate_skill_certification_commands(cfg, command_values)


def _check_skill_certification() -> dict[str, Any]:
    """Validate exact skill-certification inputs without exposing their values."""

    required_count = 8
    try:
        from arbora.core.settings import ArboraConfig as AgentConfig

        cfg = AgentConfig()
    except Exception as exc:  # noqa: BLE001 - report only the error category
        return _result(
            "skill_certification",
            "fail",
            f"skill certification configuration is invalid ({type(exc).__name__})",
            remediation=(
                "Configure the eight canonical SKILL_CERT and SKILL_VALIDATION "
                "fields through XDG AgentConfig."
            ),
            data={
                "configured_count": 0,
                "required_count": required_count,
                "ready": False,
                "redacted": True,
            },
        )

    path_values = (
        cfg.skill_cert_runtime_configuration,
        cfg.skill_cert_runtime_profile,
        cfg.skill_cert_release_spec,
        cfg.skill_cert_promotion_evidence,
    )
    command_values = (
        cfg.skill_cert_arbora_command,
        cfg.skill_validation_evidence_signer_command,
        cfg.skill_validation_evidence_verifier_command,
    )
    values = (*path_values, cfg.skill_cert_arbora_endpoint, *command_values)
    configured_count = sum(bool(value) for value in values)
    base_data = {
        "configured_count": configured_count,
        "required_count": required_count,
        "ready": False,
        "redacted": True,
    }
    if configured_count == 0:
        return _result(
            "skill_certification",
            "skip",
            "exact skill certification is not configured",
            data=base_data,
        )
    if configured_count != required_count:
        return _result(
            "skill_certification",
            "fail",
            "exact skill certification configuration is incomplete",
            remediation=(
                "Configure all eight canonical SKILL_CERT and SKILL_VALIDATION "
                "fields; partial certification authority is rejected."
            ),
            data=base_data,
        )

    try:
        _validate_skill_certification_material(cfg, path_values, command_values)
    except Exception as exc:  # noqa: BLE001 - never report paths or values
        return _result(
            "skill_certification",
            "fail",
            f"exact skill certification material is unavailable ({type(exc).__name__})",
            remediation=(
                "Generate the runtime profile, provide bounded regular release "
                "inputs, select the active loopback Arbora endpoint, and configure "
                "absolute non-shell signer and verifier argv arrays."
            ),
            data=base_data,
        )

    return _result(
        "skill_certification",
        "ok",
        "exact skill certification inputs and command boundaries are ready",
        data={
            "configured_count": required_count,
            "required_count": required_count,
            "regular_input_count": 4,
            "command_count": 3,
            "identity_authority_mode": cfg.skill_cert_identity_authority_mode,
            "identity_authority_lifecycle_owned": True,
            "identity_authority_tls_verification_required": True,
            "identity_authority_renewable_credentials_required": True,
            "ready": True,
            "redacted": True,
        },
    )


@dataclass(frozen=True)
class _CertificationRequirements:
    """The redacted production-certification facts used by the gate."""

    production_mode: bool
    release_manifest: bool
    artifacts_dir: bool
    hardware_class: bool
    load_command: bool
    metrics_command: bool
    scenario_commands: tuple[bool, ...]
    evidence_signer: bool
    evidence_verifier: bool
    prometheus_url: bool
    prometheus_tls: bool

    @property
    def values(self) -> tuple[bool, ...]:
        """Return the facts in the historical validation order."""
        return tuple(
            (
                self.production_mode,
                self.release_manifest,
                self.artifacts_dir,
                self.hardware_class,
                self.load_command,
                self.metrics_command,
            )
            + self.scenario_commands
            + (
                self.evidence_signer,
                self.evidence_verifier,
                self.prometheus_url,
                self.prometheus_tls,
            )
        )

    @property
    def configured_count(self) -> int:
        return sum(self.values)

    @property
    def configured_material(self) -> bool:
        return any(self.values[1:])


def _cert_required_values(
    cfg: Any, command_maps: tuple[Any, ...]
) -> _CertificationRequirements:
    """Collect the required production-certification facts in order."""
    return _CertificationRequirements(
        production_mode=cfg.certification_mode == "production",
        release_manifest=bool(cfg.cert_release_manifest),
        artifacts_dir=bool(cfg.cert_artifacts_dir),
        hardware_class=bool(cfg.cert_hardware_class),
        load_command=bool(cfg.cert_load_command),
        metrics_command=bool(cfg.cert_metrics_command),
        scenario_commands=tuple(bool(value) for value in command_maps),
        evidence_signer=bool(cfg.cert_evidence_signer_command),
        evidence_verifier=bool(cfg.cert_evidence_verifier_command),
        prometheus_url=bool(cfg.cert_prometheus_url),
        prometheus_tls=bool(
            cfg.cert_prometheus_tls_profile or cfg.cert_prometheus_tls_profile_ref
        ),
    )


def _cert_configuration_gate(
    cfg: Any,
    command_maps: tuple[Any, ...],
    base_data: dict[str, Any],
    required_count: int,
) -> dict[str, Any] | None:
    """Skip when nothing is configured; fail when the 13 fields are incomplete.

    Partial certification authority is never accepted: anything short of all
    13 configured fields fails rather than proceeding to the material checks.
    """
    required_values = _cert_required_values(cfg, command_maps)
    base_data.update(
        {
            "configured_count": required_values.configured_count,
            "scenario_count": len(cfg.cert_hook_commands),
            "bearer_auth_configured": bool(cfg.cert_prometheus_bearer_token_ref),
        }
    )
    configured_material = required_values.configured_material or bool(
        cfg.cert_prometheus_bearer_token_ref
    )
    if cfg.certification_mode == "disabled" and not configured_material:
        return _result(
            "production_certification",
            "skip",
            "production certification is not configured",
            data=base_data,
        )
    if base_data["configured_count"] != required_count:
        return _result(
            "production_certification",
            "fail",
            "production certification configuration is incomplete",
            remediation=(
                "Set CERTIFICATION_MODE=production and configure the release, "
                "private artifacts directory, non-identifying hardware class, "
                "load/metrics commands, all three exact scenario command maps, "
                "evidence signer/verifier commands, HTTPS Prometheus endpoint, "
                "and its dedicated TLS profile selector through AgentConfig."
            ),
            data=base_data,
        )
    return None


def _cert_verify_release_manifest(cfg: Any) -> None:
    """The release manifest must verify -- signatures included -- against the matrix."""
    from importlib.resources import as_file, files
    from pathlib import Path

    import yaml
    from scripts.release import check_compatibility as compatibility

    from arbora.deployment.certification_assets import (
        _json_without_duplicates,
        _read_regular,
    )

    release_path = Path(str(cfg.cert_release_manifest))
    release = _json_without_duplicates(
        _read_regular(
            release_path,
            limit=64 * 1024 * 1024,
            code="production_release_manifest_invalid",
        ),
        code="production_release_manifest_invalid",
    )
    if not isinstance(release, dict):
        raise RuntimeError("production_release_manifest_invalid")
    matrix_resource = files("deploy.release").joinpath("compatibility-matrix.yml")
    with as_file(matrix_resource) as matrix_path:
        matrix = yaml.safe_load(
            _read_regular(
                matrix_path,
                limit=4 * 1024 * 1024,
                code="production_compatibility_matrix_invalid",
            )
        )
        if not isinstance(matrix, dict):
            raise RuntimeError("production_compatibility_matrix_invalid")
        release_report = compatibility.verify_release_manifest(
            release,
            matrix,
            matrix_path=matrix_path,
            manifest_path=release_path,
            verify_signatures=True,
        )
    if (
        release_report.get("ok") is not True
        or release_report.get("signaturesVerified") is not True
    ):
        raise RuntimeError("production_release_signature_unverified")


def _cert_verify_artifacts_dir(cfg: Any) -> None:
    """The artifacts directory must be an empty, private, writable real directory."""
    import os
    from pathlib import Path

    artifacts_path = Path(str(cfg.cert_artifacts_dir))
    artifacts_metadata = artifacts_path.lstat()
    if (
        artifacts_path.is_symlink()
        or not stat.S_ISDIR(artifacts_metadata.st_mode)
        or stat.S_IMODE(artifacts_metadata.st_mode) & 0o077
        or not os.access(artifacts_path, os.R_OK | os.W_OK | os.X_OK)
        or any(artifacts_path.iterdir())
    ):
        raise RuntimeError("production_artifacts_directory_invalid")


def _cert_verify_bearer_token(cfg: Any) -> None:
    """A configured Prometheus bearer ref must resolve to bounded, clean material."""
    from agent_connector_sdk.credentials.runtime_references import (
        resolve_runtime_secret_reference,
    )

    if not cfg.cert_prometheus_bearer_token_ref:
        return
    token = resolve_runtime_secret_reference(cfg.cert_prometheus_bearer_token_ref)
    if (
        not token
        or len(token.encode("utf-8")) > 16_384
        or any(character in token for character in "\x00\r\n")
    ):
        raise RuntimeError("production_prometheus_bearer_token_invalid")


def _cert_resolve_prometheus_trust(cfg: Any) -> Any:
    """Resolve the certification Prometheus TLS profile.

    The ``verify_enabled`` assertion deliberately stays with the caller: the
    resolved profile owns temporary trust material and must reach the caller's
    ``finally`` for cleanup even when verification turns out to be disabled.
    """
    from arbora.core.transport import resolve_configured_tls_profile

    return resolve_configured_tls_profile(
        "certification-prometheus",
        profile_name=cfg.cert_prometheus_tls_profile,
        profile_ref=cfg.cert_prometheus_tls_profile_ref,
        config=cfg,
    )
